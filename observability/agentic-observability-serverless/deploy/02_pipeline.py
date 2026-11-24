"""Step 1b: pipeline IAM role, S3 dead-letter bucket, index templates, and the OpenSearch Ingestion pipeline.

Creates (idempotent):
  IAM role        <name>-pipeline-role   trusted by osis-pipelines.amazonaws.com; aoss:APIAccessAll on the collection,
                                         s3:PutObject on the DLQ prefix
  S3 bucket       <dlq-bucket>           rejected documents land under dlq/traces/ and dlq/logs/
  index templates                        from ../pipeline/index-templates/*.json (must exist before the first document)
  CloudWatch log group /aws/vendedlogs/OpenSearchIngestion/<name>-otlp/audit-logs
  OSIS pipeline   <name>-otlp            from ../pipeline/pipeline.yaml with placeholders filled, 1-2 units, logging on

Usage:
  python deploy/02_pipeline.py --name agentobs --region us-east-1 --profile default --dlq-bucket <unique-bucket-name>
Prints the ingest endpoint when ACTIVE (10-16 minutes; the endpoint may refuse connections for a few more minutes).
"""
import argparse
import json
import pathlib
import sys
import time

import boto3
from botocore.exceptions import ClientError
from opensearchpy import OpenSearch, RequestsAWSV4SignerAuth, RequestsHttpConnection

HERE = pathlib.Path(__file__).resolve().parent
PIPELINE_DIR = HERE.parent / "pipeline"


def code(e):
    return getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="agentobs")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--dlq-bucket", required=True, help="globally unique S3 bucket name for rejected documents")
    ap.add_argument("--min-units", type=int, default=1)
    ap.add_argument("--max-units", type=int, default=2)
    ap.add_argument("--tags", default="", help="extra tags k=v,k=v")
    a = ap.parse_args()

    s = boto3.Session(profile_name=a.profile, region_name=a.region)
    acct = s.client("sts").get_caller_identity()["Account"]
    aoss, iam, osis, s3, logs = (s.client(x) for x in ("opensearchserverless", "iam", "osis", "s3", "logs"))
    col, role_name, pipe = f"{a.name}-ts", f"{a.name}-pipeline-role", f"{a.name}-otlp"
    role_arn = f"arn:aws:iam::{acct}:role/{role_name}"
    tags = [{"Key": "project", "Value": "agentic-observability-serverless"}] + [
        {"Key": kv.split("=", 1)[0], "Value": kv.split("=", 1)[1]} for kv in a.tags.split(",") if "=" in kv]

    d = aoss.batch_get_collection(names=[col])["collectionDetails"][0]
    endpoint, col_arn = d["collectionEndpoint"], d["arn"]

    # 1. IAM role
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "osis-pipelines.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": acct},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:osis:{a.region}:{acct}:pipeline/*"}}}]}
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
                        Description="OpenSearch Ingestion pipeline role (agentic observability sample)", Tags=tags)
        print(f"iam role {role_name}: created")
    perm = {"Version": "2012-10-17", "Statement": [
        {"Sid": "ServerlessApi", "Effect": "Allow", "Action": ["aoss:APIAccessAll"], "Resource": col_arn},
        {"Sid": "DiscoverCollection", "Effect": "Allow", "Action": ["aoss:BatchGetCollection"], "Resource": "*"},
        {"Sid": "DeadLetterQueue", "Effect": "Allow", "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
         "Resource": f"arn:aws:s3:::{a.dlq_bucket}/dlq/*"},
    ]}
    iam.put_role_policy(RoleName=role_name, PolicyName="pipeline-sink", PolicyDocument=json.dumps(perm))
    print(f"iam role {role_name}: inline policy set")

    # 2. DLQ bucket
    try:
        s3.head_bucket(Bucket=a.dlq_bucket)
        print(f"s3 bucket {a.dlq_bucket}: exists")
    except ClientError:
        kw = {"Bucket": a.dlq_bucket}
        if a.region != "us-east-1":
            kw["CreateBucketConfiguration"] = {"LocationConstraint": a.region}
        s3.create_bucket(**kw)
        s3.put_public_access_block(Bucket=a.dlq_bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_tagging(Bucket=a.dlq_bucket, Tagging={"TagSet": tags})
        print(f"s3 bucket {a.dlq_bucket}: created (public access blocked)")

    # 3. index templates (before any document lands)
    client = OpenSearch(hosts=[{"host": endpoint.replace("https://", ""), "port": 443}],
                        http_auth=RequestsAWSV4SignerAuth(s.get_credentials(), a.region, "aoss"),
                        use_ssl=True, verify_certs=True, connection_class=RequestsHttpConnection, timeout=60)
    for f in sorted((PIPELINE_DIR / "index-templates").glob("*.json")):
        client.indices.put_index_template(name=f.stem, body=json.loads(f.read_text(encoding="utf-8")))
        print(f"index template {f.stem}: applied")

    # 4. pipeline
    body = (PIPELINE_DIR / "pipeline.yaml").read_text(encoding="utf-8")
    for k, v in {"<prefix>": a.name, "<collection-id>": d["id"], "<region>": a.region, "<account-id>": acct,
                 "<pipeline-role>": role_name, "<dlq-bucket>": a.dlq_bucket}.items():
        body = body.replace(k, v)
    body = body.replace(f"https://{d['id']}.aoss.{a.region}.on.aws", endpoint)
    v = osis.validate_pipeline(PipelineConfigurationBody=body)
    if not v.get("isValid", True):
        print("pipeline configuration INVALID:", json.dumps(v.get("Errors"), indent=1))
        return 1
    out = pathlib.Path.cwd() / f"rendered-{pipe}.yaml"  # written to the current directory, not the sample
    out.write_text(body, encoding="utf-8")

    log_group = f"/aws/vendedlogs/OpenSearchIngestion/{pipe}/audit-logs"
    try:
        logs.create_log_group(logGroupName=log_group)
        logs.put_retention_policy(logGroupName=log_group, retentionInDays=14)
    except ClientError as e:
        if code(e) != "ResourceAlreadyExistsException":
            raise
    try:
        osis.get_pipeline(PipelineName=pipe)
        print(f"pipeline {pipe}: exists")
    except ClientError as e:
        if code(e) != "ResourceNotFoundException":
            raise
        time.sleep(10)  # IAM propagation
        osis.create_pipeline(PipelineName=pipe, MinUnits=a.min_units, MaxUnits=a.max_units, PipelineConfigurationBody=body,
                             LogPublishingOptions={"IsLoggingEnabled": True, "CloudWatchLogDestination": {"LogGroup": log_group}},
                             Tags=tags)
        print(f"pipeline {pipe}: create requested (rendered config saved to {out})")

    for _ in range(180):  # up to 30 min; creation typically takes 10-16 min
        p = osis.get_pipeline(PipelineName=pipe)["Pipeline"]
        if p["Status"] == "ACTIVE":
            print(f"\npipeline {pipe}: ACTIVE")
            for u in p.get("IngestEndpointUrls", []):
                print(f"PIPELINE_ENDPOINT=https://{u}")
            print("Allow a few minutes before the endpoint accepts connections.")
            return 0
        if p["Status"].endswith("FAILED"):
            print(f"pipeline {pipe}: {p['Status']} {p.get('StatusReason')}")
            return 1
        time.sleep(10)
    print("pipeline not ACTIVE yet; re-run to continue waiting")
    return 1


if __name__ == "__main__":
    sys.exit(main())
