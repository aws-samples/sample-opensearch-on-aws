"""Step 0: show what will be deployed, where, with which models and versions, and what it costs. Read-only.

Creates nothing. Run it first and confirm the plan before running 01-03. A coding agent deploying this sample should
show this output to the user and wait for an explicit "yes" before creating anything.

Usage:
  python deploy/00_preflight.py --name agentobs --region us-east-1 --profile default --dlq-bucket <unique-bucket-name>
  (outside the US, add --model-prefix eu / apac / global)
Exit code: 0 = ready (and, when run in a terminal, you answered yes), 1 = a blocking problem or you answered no.
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


def main():
    host = load_host_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="agentobs")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--dlq-bucket", default="<dlq-bucket>")
    ap.add_argument("--model-prefix", default=host.DEFAULT_MODEL_PREFIX)
    ap.add_argument("--instance-type", default="r8a.2xlarge")
    ap.add_argument("--ami-param", default="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
    a = ap.parse_args()
    problems = []

    # local tools
    py = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 11):
        problems.append(f"Python {py} found; 3.11 or later is required")
    boto3_v = pkg_version("boto3") or "not installed"
    if boto3_v == "not installed" or version_tuple(boto3_v) < MIN_BOTO3:
        problems.append(f"boto3 {boto3_v}; run pip install -r deploy/requirements.txt (needs >= 1.44.0)")
    osp_v = pkg_version("opensearch-py") or "not installed"
    if osp_v == "not installed":
        problems.append("opensearch-py not installed; run pip install -r deploy/requirements.txt")

    # identity
    try:
        s = boto3.Session(profile_name=a.profile, region_name=a.region)
        ident = s.client("sts").get_caller_identity()
        account, caller = ident["Account"], ident["Arn"]
    except (BotoCoreError, ClientError) as e:
        print(f"Cannot read AWS credentials for profile {a.profile or '(default chain)'}: {e}")
        return 1

    # models
    bedrock = s.client("bedrock")
    agent_models = host.model_profiles(a.model_prefix)
    judge_model = f"{a.model_prefix}.{JUDGE_MODEL_ID}"
    model_state = {}
    for m in dict.fromkeys(agent_models + [judge_model]):
        try:
            model_state[m] = bedrock.get_inference_profile(inferenceProfileIdentifier=m).get("status", "ACTIVE")
        except ClientError as e:
            model_state[m] = f"NOT AVAILABLE ({e.response['Error']['Code']})"
            problems.append(f"Bedrock inference profile {m} is not available in {a.region}; "
                            f"try another --model-prefix or region")

    # versions that will be deployed
    compose = (HERE.parent / "demo-app" / "compose.bedrock.yaml").read_text(encoding="utf-8")
    litellm_image = re.search(r"^\s*image:\s*(\S+)", compose, re.M).group(1)
    try:
        ami = s.client("ssm").get_parameter(Name=a.ami_param)["Parameter"]["Value"]
    except ClientError as e:
        ami = f"unknown ({e.response['Error']['Code']})"

    # names already in use
    existing = []
    try:
        if s.client("opensearchserverless").batch_get_collection(names=[f"{a.name}-ts"]).get("collectionDetails"):
            existing.append(f"collection {a.name}-ts")
    except ClientError:
        pass
    try:
        s.client("osis").get_pipeline(PipelineName=f"{a.name}-otlp")
        existing.append(f"pipeline {a.name}-otlp")
    except ClientError:
        pass

    n = a.name
    weights = {agent_models[0]: "70%", agent_models[1]: "30%"}
    print(f"""
Agentic observability on Amazon OpenSearch Serverless: deployment plan
Nothing has been created yet.

Where
  AWS account     {account}
  Signed in as    {caller}
  Region          {a.region}
  Name prefix     {n}

What will be created (every resource tagged project=agentic-observability-serverless)
  01_collection   collection group {n}, time series collection {n}-ts, encryption/network/data access policies
  02_pipeline     IAM role {n}-pipeline-role, S3 bucket {a.dlq_bucket}, index templates,
                  OpenSearch Ingestion pipeline {n}-otlp and its CloudWatch log group
  03_host         IAM role {n}-host-role, security group {n}-host-sg (no inbound rules),
                  EC2 instance {n}-shop ({a.instance_type}, Amazon Linux 2023 {ami})
  04_ui           OpenSearch UI application and workspace (console steps in deploy/04_ui.md)

Models on Amazon Bedrock""")
    for m in agent_models:
        print(f"  assistant       {m:<40} {weights[m]:>4}  {model_state[m]}")
    print(f"  evaluation      {judge_model:<40}       {model_state[judge_model]}")
    print(f"""
Versions
  OpenTelemetry Demo   {host.DEMO_TAG}
  LiteLLM proxy        {litellm_image}
  Docker Compose       {host.COMPOSE_VERSION}
  Python / boto3 / opensearch-py on this machine: {py} / {boto3_v} / {osp_v}

Cost while running   estimate for us-east-1 list prices at the load generator's default rate: about USD 30-40/day
                     (EC2 ~11, pipeline 6-12, collection a few, assistant models 8-10); online evaluation adds about
                     1 cent per scored conversation (~USD 30/day if run continuously). Prices differ by region,
                     instance type and traffic; check the AWS Pricing Calculator for {a.region}.
                     deploy/99_cleanup.py deletes everything the scripts created.""")
    if existing:
        print("\nAlready exists with this prefix (will be reused, not recreated): " + ", ".join(existing))
    if problems:
        print("\nFix before deploying:")
        for p in problems:
            print(f"  - {p}")
        return 1
    if sys.stdin.isatty():
        try:
            answer = input("\nCreate these resources? [yes/no] ").strip().lower()
        except EOFError:  # no keyboard attached (Windows reports NUL as a terminal)
            answer = None
        if answer is not None:
            if answer not in ("y", "yes"):
                print("Stopped. Nothing was created.")
                return 1
            print("Confirmed. Continue with deploy/01_collection.py.")
            return 0
    print("\nReady. Confirm with the account owner before running deploy/01_collection.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
