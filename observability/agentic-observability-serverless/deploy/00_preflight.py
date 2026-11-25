"""Step 0: show what will be deployed, where, with which models and versions, and what it costs. Read-only.

Creates nothing and never waits for input. Run it first and confirm the plan before running 01-04. A coding agent
deploying this sample should show this output to the user, ask the questions at the end, and wait for "yes".

Usage:
  python deploy/00_preflight.py --name agentobs --region us-east-1 --profile default \
      [--console-principal <ARN you sign in to the AWS console with>] [--subnet subnet-...]
  (outside the US, add --model-prefix eu / apac / global)
With --console-principal it prints the exact commands for steps 1-4. Exit code: 0 = ready, 1 = a blocking problem.
"""
import argparse
import importlib.metadata as md
import importlib.util
import pathlib
import re
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

HERE = pathlib.Path(__file__).resolve().parent
MIN_BOTO3 = (1, 44, 0)
JUDGE_MODEL_ID = "anthropic.claude-sonnet-5-5"   # same as evaluation/score_live.py


def load_host_settings():
    spec = importlib.util.spec_from_file_location("host_settings", HERE / "03_host.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # main() is guarded; importing only reads the constants
    return mod


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def pkg_version(name):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def model_label(profile_id):
    # "us.anthropic.claude-haiku-5-5" -> "Claude Haiku 5.5"
    m = re.search(r"claude-([a-z]+)-(\d+)-(\d+)", profile_id)
    return f"Claude {m.group(1).title()} {m.group(2)}.{m.group(3)}" if m else profile_id


def principal_for_data_access(arn):
    # a role session (arn:aws:sts::<acct>:assumed-role/<Role>/<session>) is granted through its role ARN
    m = re.fullmatch(r"arn:aws:sts::(\d+):assumed-role/([^/]+)/.+", arn)
    return f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}" if m else arn


def public_subnets(ec2):
    """Subnets whose route table sends 0.0.0.0/0 to an internet gateway; default VPC first, most free IPs first."""
    vpcs = {v["VpcId"]: v.get("IsDefault", False) for v in ec2.describe_vpcs()["Vpcs"]}
    tables = ec2.describe_route_tables()["RouteTables"]

    def to_igw(rt):
        return any(r.get("DestinationCidrBlock") == "0.0.0.0/0" and r.get("GatewayId", "").startswith("igw-")
                   and r.get("State") == "active" for r in rt["Routes"])

    explicit = {a["SubnetId"]: rt for rt in tables for a in rt.get("Associations", []) if a.get("SubnetId")}
    main = {rt["VpcId"]: rt for rt in tables if any(a.get("Main") for a in rt.get("Associations", []))}
    found = []
    for sn in ec2.describe_subnets()["Subnets"]:
        rt = explicit.get(sn["SubnetId"]) or main.get(sn["VpcId"])
        if rt and to_igw(rt):
            found.append((not vpcs.get(sn["VpcId"], False), -sn["AvailableIpAddressCount"], sn["SubnetId"],
                          sn["AvailabilityZone"], vpcs.get(sn["VpcId"], False)))
    return [(sid, az, is_default) for _, _, sid, az, is_default in sorted(found)]


def main():
    host = load_host_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="agentobs")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--dlq-bucket", default=None, help="default: <name>-dlq-<account-id>")
    ap.add_argument("--console-principal", default=None,
                    help="the ARN you sign in to the AWS console with (user ARN, or the assumed-role session ARN)")
    ap.add_argument("--subnet", default=None, help="public subnet for the EC2 host; default: one found in the account")
    ap.add_argument("--model-prefix", default=host.DEFAULT_MODEL_PREFIX)
    ap.add_argument("--instance-type", default="r8a.2xlarge")
    a = ap.parse_args()
    problems = []

    py = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 11):
        problems.append(f"Python {py} found; 3.11 or later is required")
    boto3_v = pkg_version("boto3")
    if not boto3_v or version_tuple(boto3_v) < MIN_BOTO3:
        problems.append(f"boto3 {boto3_v or 'missing'}: run pip install -r deploy/requirements.txt (needs 1.44.0+)")
    if not pkg_version("opensearch-py"):
        problems.append("opensearch-py missing: run pip install -r deploy/requirements.txt")

    try:
        cli_region = boto3.Session(profile_name=a.profile).region_name
        s = boto3.Session(profile_name=a.profile, region_name=a.region)
        ident = s.client("sts").get_caller_identity()
        account, caller = ident["Account"], ident["Arn"]
    except (BotoCoreError, ClientError) as e:
        print(f"Cannot read AWS credentials for profile {a.profile or '(default chain)'}: {e}")
        return 1

    agent_models = host.model_profiles(a.model_prefix)
    judge_model = f"{a.model_prefix}.{JUDGE_MODEL_ID}"
    bedrock = s.client("bedrock")
    for m in dict.fromkeys(agent_models + [judge_model]):
        try:
            bedrock.get_inference_profile(inferenceProfileIdentifier=m)
        except ClientError:
            problems.append(f"Bedrock model {m} is not available in {a.region}: try another --model-prefix or region")

    subnet, subnet_note = a.subnet, "given with --subnet"
    if not subnet:
        try:
            ec2 = s.client("ec2")
            offered = {o["Location"] for o in ec2.describe_instance_type_offerings(
                LocationType="availability-zone",
                Filters=[{"Name": "instance-type", "Values": [a.instance_type]}])["InstanceTypeOfferings"]}
            candidates = [c for c in public_subnets(ec2) if c[1] in offered]
        except ClientError as e:
            candidates, subnet_note = [], f"could not list subnets ({e.response['Error']['Code']})"
        if candidates:
            subnet, az, is_default = candidates[0]
            others = ", ".join(c[0] for c in candidates[1:3])
            subnet_note = (f"{az}, {'default VPC' if is_default else 'public route'}"
                           + (f"; if {a.instance_type} is out of capacity there, try {others}" if others else ""))
        else:
            problems.append(f"no public subnet (route 0.0.0.0/0 to an internet gateway) in an Availability Zone that "
                            f"offers {a.instance_type}; create one, pass --subnet, or choose another --instance-type")

    compose = (HERE.parent / "demo-app" / "compose.bedrock.yaml").read_text(encoding="utf-8")
    lv = re.search(r"LiteLLM (\d+(?:\.\d+)+)", compose)
    litellm = lv.group(1) if lv else re.search(r"^\s*image:\s*(\S+)", compose, re.M).group(1)

    existing = []
    try:
        if s.client("opensearchserverless").batch_get_collection(names=[f"{a.name}-ts"]).get("collectionDetails"):
            existing.append(f"{a.name}-ts")
    except ClientError:
        pass
    try:
        s.client("osis").get_pipeline(PipelineName=f"{a.name}-otlp")
        existing.append(f"{a.name}-otlp")
    except ClientError:
        pass

    n = a.name
    bucket = a.dlq_bucket or f"{n}-dlq-{account}"
    region_note = f"  (your CLI default is {cli_region}; pass --region to change)" if cli_region and cli_region != a.region else ""
    models = " and ".join(dict.fromkeys(model_label(m) for m in agent_models + [judge_model]))

    status = "not ready, see below" if problems else "ready to deploy"
    print(f"""
Agentic observability sample: {status}. Nothing has been created yet.

  Account   {account}, signed in as {caller.split('/')[-1]}
  Region    {a.region}{region_note}
  Creates   collection {n}-ts, pipeline {n}-otlp, EC2 {n}-shop ({a.instance_type}),
            bucket {bucket}, IAM roles and an OpenSearch UI app
  Subnet    {subnet or '-'} ({subnet_note})
  Models    {models} on Amazon Bedrock ({a.model_prefix}. profiles)
  Versions  OpenTelemetry Demo {host.DEMO_TAG}, LiteLLM {litellm}, Docker Compose {host.COMPOSE_VERSION}
  Cost      about USD 30-40/day in us-east-1 while it runs (varies by region and traffic);
            evaluation adds about 1 cent per conversation. deploy/99_cleanup.py removes it all.""")
    if existing:
        print(f"\n  Already exists, will be reused: {', '.join(existing)}. Use another --name for a fresh stack.")
    if problems:
        print("\nFix first:")
        for p in problems:
            print(f"  - {p}")
        return 1

    if not a.console_principal:
        print(f"""
Questions:
  1. Which IAM user or role do you sign in to the AWS console with? OpenSearch UI shows data only to
     identities in the collection's data access policy. (Scripts run as {caller}.)
  2. Go ahead? (yes/no)
Re-run with --console-principal <ARN> to get the exact commands.""")
        return 0

    console = a.console_principal.strip()
    principals = ",".join(dict.fromkeys([principal_for_data_access(caller), principal_for_data_access(console)]))
    admins = ",".join(dict.fromkeys([console, caller]))
    common = f"--name {n} --region {a.region}" + (f" --profile {a.profile}" if a.profile else "")
    print(f"""
Go ahead? (yes/no). After yes, run in order:
  python deploy/01_collection.py {common} --principals {principals}
  python deploy/02_pipeline.py {common} --dlq-bucket {bucket}
  python deploy/03_host.py {common} --subnet {subnet}{'' if a.model_prefix == 'us' else ' --model-prefix ' + a.model_prefix}
  python deploy/04_ui.py {common} --admins {admins}""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
