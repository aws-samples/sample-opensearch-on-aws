"""Step 2: EC2 host running the OpenTelemetry Demo with the additions in ../demo-app/.

Creates (idempotent):
  IAM role + instance profile  <name>-host-role   osis:Ingest on the pipeline, bedrock:InvokeModel* on the two model
                                                  inference profiles, AmazonSSMManagedInstanceCore
  Security group               <name>-host-sg     no inbound rules
  EC2 instance                 <name>-shop        r8a.2xlarge, Amazon Linux 2023, 100 GiB gp3, IMDSv2 hop limit 2
User data (runs once at first boot, log: /var/log/bootstrap.log on the host):
  installs Docker + Compose, clones the demo at tag 3.1.0, writes the Collector extras (two OSIS exporters in their
  own pipelines, bounded queues, the `error` attribute transform), copies ../demo-app/ (LiteLLM proxy, patches,
  env overrides), generates the LiteLLM master key, extends the Makefile and runs `make start-agentic`.

Usage:
  python deploy/03_host.py --name agentobs --region us-east-1 --profile default --subnet subnet-xxxxxxxx
First spans reach the collection about 15-25 minutes after launch (image pulls). Reach the shop UI afterwards with
  aws ssm start-session --target <instance-id> --document-name AWS-StartPortForwardingSession \
      --parameters "portNumber=8080,localPortNumber=8080"      -> http://localhost:8080
"""
import argparse
import base64
import gzip
import json
import pathlib
import sys
import time

import boto3
from botocore.exceptions import ClientError

HERE = pathlib.Path(__file__).resolve().parent
DEMO_APP = HERE.parent / "demo-app"
DEMO_TAG = "3.1.0"
MODELS = ("us.anthropic.claude-haiku-5-5", "us.anthropic.claude-sonnet-5-5")


def code(e):
    return getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)


def collector_extras(region, endpoint, prefix):
    # Loaded last by the demo's Collector. Array keys REPLACE upstream values, so upstream exporters are repeated.
    return f"""# Export the demo's telemetry to Amazon OpenSearch Ingestion (SigV4 from the instance role, IMDS hop limit 2).
extensions:
  sigv4auth:
    region: {region}
    service: osis
processors:
  batch: {{}}
  transform/error-attr:
    error_mode: ignore
    trace_statements:
      - context: span
        statements:
          - set(attributes["error.message"], attributes["error"]) where attributes["error"] != nil and attributes["error.message"] == nil
          - delete_key(attributes, "error") where attributes["error"] != nil
exporters:
  otlphttp/osis-traces:
    traces_endpoint: "https://{endpoint}/{prefix}/v1/traces"
    auth: {{ authenticator: sigv4auth }}
    compression: none
    sending_queue: {{ enabled: true, num_consumers: 4, queue_size: 2000 }}
    retry_on_failure: {{ enabled: true, max_elapsed_time: 120s }}
  otlphttp/osis-logs:
    logs_endpoint: "https://{endpoint}/{prefix}/v1/logs"
    auth: {{ authenticator: sigv4auth }}
    compression: none
    sending_queue: {{ enabled: true, num_consumers: 2, queue_size: 2000 }}
    retry_on_failure: {{ enabled: true, max_elapsed_time: 120s }}
service:
  extensions: [opamp, sigv4auth]
  pipelines:
    traces:
      exporters: [debug, otlp_grpc/jaeger, span_metrics]
    # Same processor chain as the demo's own traces pipeline (resource detection, GenAI normalisation of Traceloop
    # spans -> gen_ai.operation.name, PII redaction), plus our error-attribute transform and batching. The
    # gen_ai_normalizer step is required: the OSIS pipeline's root-span rule keys on gen_ai.operation.name == invoke_workflow.
    traces/osis:
      receivers: [otlp]
      processors: [resource_detection, memory_limiter, transform/sanitize_spans, gen_ai_normalizer, transform/redact_sensitive_data, redaction, transform/error-attr, batch]
      exporters: [otlphttp/osis-traces]
    logs:
      exporters: [debug, opensearch]
    logs/osis:
      receivers: [otlp]
      processors: [resource_detection, memory_limiter, transform/sanitize_logs, batch]
      exporters: [otlphttp/osis-logs]
"""


def user_data(region, endpoint, prefix, files, s3_prefix=None):
    """files: {relative path under demo-app: content} embedded as base64, or fetched from s3_prefix on the host."""
    if s3_prefix:
        writes = f"aws s3 cp --recursive {s3_prefix}/ . --region {region}   # AWS CLI ships with Amazon Linux 2023"
    else:
        writes = []
        for rel, content in files.items():
            b64 = base64.b64encode(content.encode("utf-8")).decode()
            writes.append(f"mkdir -p \"$(dirname {rel})\" && echo '{b64}' | base64 -d > {rel}")
        writes = "\n".join(writes)
    extras_b64 = base64.b64encode(collector_extras(region, endpoint, prefix).encode()).decode()
    return f"""#!/bin/bash
set -euxo pipefail
exec > >(tee -a /var/log/bootstrap.log) 2>&1
dnf install -y docker git jq make python3 >/dev/null
systemctl enable --now docker
mkdir -p /usr/libexec/docker/cli-plugins
curl -sSL https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64 -o /usr/libexec/docker/cli-plugins/docker-compose
chmod +x /usr/libexec/docker/cli-plugins/docker-compose
cd /root
[ -d opentelemetry-demo ] || git clone --depth 1 --branch {DEMO_TAG} https://github.com/open-telemetry/opentelemetry-demo.git
cd /root/opentelemetry-demo
echo '{extras_b64}' | base64 -d > src/otel-collector/otelcol-config-extras.yml
{writes}
# LiteLLM master key: generated once, root-only, never logged (xtrace off while handling it). The agent gets it as API_KEY.
set +x
umask 077; python3 -c 'import secrets; print("LITELLM_MASTER_KEY=sk-" + secrets.token_hex(32))' > litellm/litellm.env; umask 022
KEY=$(sed -n 's/^LITELLM_MASTER_KEY=//p' litellm/litellm.env)
sed -i "s|^API_KEY=.*|API_KEY=$KEY|" .env.override
unset KEY
set -x
# load our two overlays through the demo's own target so .env and .env.override are both applied
sed -i 's|DOCKER_COMPOSE_FILES_AGENT=-f compose.agent.yaml|DOCKER_COMPOSE_FILES_AGENT=-f compose.agent.yaml -f compose.bedrock.yaml -f compose.patches.yaml|' Makefile
export AWS_REGION={region}
make start-agentic
touch /root/.bootstrap-done
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="agentobs")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--subnet", required=True, help="public subnet with a route to the internet (image pulls, Bedrock, OSIS)")
    ap.add_argument("--instance-type", default="r8a.2xlarge")
    ap.add_argument("--ami-param", default="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
    ap.add_argument("--tags", default="", help="extra tags k=v,k=v")
    ap.add_argument("--demo-app-s3", default=None, metavar="s3://bucket/prefix",
                    help="upload demo-app/ here and let the host fetch it (avoids the 16 KiB user-data limit); the DLQ bucket works")
    a = ap.parse_args()

    s = boto3.Session(profile_name=a.profile, region_name=a.region)
    acct = s.client("sts").get_caller_identity()["Account"]
    ec2, iam, osis, ssm = (s.client(x) for x in ("ec2", "iam", "osis", "ssm"))
    role_name, sg_name, inst_name, pipe = f"{a.name}-host-role", f"{a.name}-host-sg", f"{a.name}-shop", f"{a.name}-otlp"
    tags = [{"Key": "project", "Value": "agentic-observability-serverless"}] + [
        {"Key": kv.split("=", 1)[0], "Value": kv.split("=", 1)[1]} for kv in a.tags.split(",") if "=" in kv]

    p = osis.get_pipeline(PipelineName=pipe)["Pipeline"]
    if p["Status"] != "ACTIVE":
        print(f"pipeline {pipe} is {p['Status']}; wait for ACTIVE and re-run")
        return 2
    endpoint, pipe_arn = p["IngestEndpointUrls"][0], p["PipelineArn"]

    # IAM role + instance profile
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        existing_role = iam.get_role(RoleName=role_name)["Role"]
        role_tags = {t["Key"]: t["Value"] for t in existing_role.get("Tags", [])}
        if role_tags.get("project") != "agentic-observability-serverless":
            print(f"iam role {role_name} already exists and was not created by this sample (no project tag); "
                  f"choose another --name instead of modifying it")
            return 1
        print(f"iam role {role_name}: exists (created by this sample)")
    except ClientError as e:
        if code(e) != "NoSuchEntity":
            raise
        iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust),
                        Description="Demo host: OSIS ingest, Bedrock invoke, SSM", Tags=tags)
        print(f"iam role {role_name}: created")
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "IngestToPipeline", "Effect": "Allow", "Action": ["osis:Ingest"], "Resource": pipe_arn},
        {"Sid": "BedrockModels", "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": [f"arn:aws:bedrock:{a.region}:{acct}:inference-profile/{m}" for m in MODELS]
         + ["arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-*", "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-*"]},
    ]}
    iam.put_role_policy(RoleName=role_name, PolicyName="demo-host", PolicyDocument=json.dumps(policy))
    iam.attach_role_policy(RoleName=role_name, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    try:
        iam.get_instance_profile(InstanceProfileName=role_name)
    except ClientError as e:
        if code(e) != "NoSuchEntity":
            raise
        iam.create_instance_profile(InstanceProfileName=role_name, Tags=tags)
        iam.add_role_to_instance_profile(InstanceProfileName=role_name, RoleName=role_name)
        print(f"instance profile {role_name}: created")
        time.sleep(12)

    # security group, no inbound
    vpc = ec2.describe_subnets(SubnetIds=[a.subnet])["Subnets"][0]["VpcId"]
    sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [sg_name]}, {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
    if sgs:
        sg_id = sgs[0]["GroupId"]
    else:
        sg_id = ec2.create_security_group(GroupName=sg_name, Description="demo host: no inbound", VpcId=vpc,
                                          TagSpecifications=[{"ResourceType": "security-group", "Tags": tags + [{"Key": "Name", "Value": sg_name}]}])["GroupId"]
        print(f"security group {sg_name}: created ({sg_id}), no inbound rules")

    existing = ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [inst_name]},
                                               {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
    if any(i for r in existing["Reservations"] for i in r["Instances"]):
        print(f"instance {inst_name}: exists")
        return 0

    # files from demo-app/ to drop into the checkout: either embedded in the user data (default; EC2 caps user data at
    # 16 KiB after gzip) or uploaded to S3 and fetched by the host (--demo-app-s3, no size ceiling)
    files = {
        "compose.bedrock.yaml": (DEMO_APP / "compose.bedrock.yaml").read_text(encoding="utf-8"),
        "compose.patches.yaml": (DEMO_APP / "compose.patches.yaml").read_text(encoding="utf-8"),
        "litellm/config.yaml": (DEMO_APP / "litellm" / "config.yaml").read_text(encoding="utf-8").replace("us-east-1", a.region),
        "patches/agent/llm.py": (DEMO_APP / "patches" / "agent" / "llm.py").read_text(encoding="utf-8"),
        "patches/loadgen/locustfile.py": (DEMO_APP / "patches" / "loadgen" / "locustfile.py").read_text(encoding="utf-8"),
        "patches/loadgen/agent_prompts.py": (DEMO_APP / "patches" / "loadgen" / "agent_prompts.py").read_text(encoding="utf-8"),
        ".env.override": (DEMO_APP / "env.override.example").read_text(encoding="utf-8"),
    }
    s3_prefix = None
    if a.demo_app_s3:
        bucket, _, prefix = a.demo_app_s3.replace("s3://", "").partition("/")
        prefix = prefix.rstrip("/") or "demo-app"
        s3c = s.client("s3")
        for rel, content in files.items():
            s3c.put_object(Bucket=bucket, Key=f"{prefix}/{rel}", Body=content.encode("utf-8"))
        s3_prefix = f"s3://{bucket}/{prefix}"
        iam.put_role_policy(RoleName=role_name, PolicyName="demo-app-files", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/{prefix}/*"},
                {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{bucket}"}]}))
        print(f"demo-app files uploaded to {s3_prefix}")
        files = {}
    ud = user_data(a.region, endpoint, a.name, files, s3_prefix)
    out = pathlib.Path.cwd() / f"rendered-{a.name}-user-data.sh"  # written to the current directory, not the sample
    out.write_text(ud, encoding="utf-8")
    # EC2 accepts at most 16 KiB of user data; the embedded demo-app files exceed that as text, so send it gzipped.
    # cloud-init detects and decompresses gzip transparently.
    ud_gz = gzip.compress(ud.encode("utf-8"), compresslevel=9)
    if len(ud_gz) > 16384:
        print(f"user data is {len(ud_gz)} bytes gzipped, over the 16384-byte EC2 limit; re-run with --demo-app-s3 s3://<bucket>/demo-app")
        return 1
    print(f"user data: {len(ud)} bytes raw, {len(ud_gz)} bytes gzipped")
    ami = ssm.get_parameter(Name=a.ami_param)["Parameter"]["Value"]
    r = ec2.run_instances(
        ImageId=ami, InstanceType=a.instance_type, MinCount=1, MaxCount=1,
        NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": a.subnet, "Groups": [sg_id], "AssociatePublicIpAddress": True}],
        IamInstanceProfile={"Name": role_name}, UserData=ud_gz,
        BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 100, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}],
        MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 2, "HttpEndpoint": "enabled"},
        TagSpecifications=[{"ResourceType": t, "Tags": tags + [{"Key": "Name", "Value": inst_name}]} for t in ("instance", "volume")],
    )
    iid = r["Instances"][0]["InstanceId"]
    print(f"instance {inst_name}: launched {iid} ({a.instance_type}, {ami}); user data saved to {out}")
    for n in range(40):
        time.sleep(15)
        info = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [iid]}])["InstanceInformationList"]
        if info and info[0]["PingStatus"] == "Online":
            print(f"instance {iid}: SSM online; bootstrap continues (watch /var/log/bootstrap.log, ~15-25 min)")
            print(f"INSTANCE_ID={iid}")
            return 0
    print(f"INSTANCE_ID={iid} (SSM not online yet)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
