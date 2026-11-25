"""Step 0: show what will be deployed, where, with which models and versions, and what it costs. Read-only.

Creates nothing. Run it first and confirm the plan before running 01-03. A coding agent deploying this sample should
show this output to the user, ask the two questions at the end, and wait for an explicit "yes".

Usage:
  python deploy/00_preflight.py --name agentobs --region us-east-1 --profile default
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


def model_label(profile_id):
    # "us.anthropic.claude-haiku-5-5" -> "Claude Haiku 5.5"
    m = re.search(r"claude-([a-z]+)-(\d+)-(\d+)", profile_id)
    return f"Claude {m.group(1).title()} {m.group(2)}.{m.group(3)}" if m else profile_id


def principal_for_data_access(arn):
    # a role session (arn:aws:sts::<acct>:assumed-role/<Role>/<session>) is granted through its role ARN
    m = re.fullmatch(r"arn:aws:sts::(\d+):assumed-role/([^/]+)/.+", arn)
    return f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}" if m else arn


def main():
    host = load_host_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="agentobs")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--dlq-bucket", default=None, help="default: <name>-dlq-<account-id>")
    ap.add_argument("--principals", default=None, help="the value you will pass to 01_collection.py, if known")
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
    data_access = a.principals or principal_for_data_access(caller)

    status = "not ready, see below" if problems else "ready to deploy"
    print(f"""
Agentic observability sample: {status}. Nothing has been created yet.

  Account   {account}, signed in as {caller.split('/')[-1]}
  Region    {a.region}{region_note}
  Creates   collection {n}-ts, pipeline {n}-otlp, EC2 {n}-shop ({a.instance_type}),
            bucket {bucket}, IAM roles and an OpenSearch UI app
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
    print(f"""
Two questions:
  1. Which IAM user or role do you sign in to the AWS console with? OpenSearch UI needs it in
     --principals to show any data. So far: {data_access}
  2. Go ahead? (yes/no)""")

    if sys.stdin.isatty():
        try:
            console = input("\nConsole user or role ARN (Enter to keep the list above): ").strip()
            answer = input("Go ahead? [yes/no] ").strip().lower()
        except EOFError:  # no keyboard attached (Windows reports NUL as a terminal)
            return 0
        if answer not in ("y", "yes"):
            print("Stopped. Nothing was created.")
            return 1
        principals = ",".join(dict.fromkeys(p for p in (data_access, principal_for_data_access(console)) if p))
        profile = f" --profile {a.profile}" if a.profile else ""
        print(f"\nNext: python deploy/01_collection.py --name {n} --region {a.region}{profile} --principals {principals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
