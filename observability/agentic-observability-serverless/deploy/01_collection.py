"""Step 1a: OpenSearch Serverless collection group (next generation), time series collection, and its policies.

Creates (idempotent, re-run safe):
  collection group  <name>        NEXTGEN, standby replicas enabled, OCU limits from --max-ocu / --min-search-ocu (0, 2, 4, 8, 16, ...)
  encryption policy <name>-enc    AWS-owned key
  network policy    <name>-net    public endpoint (restrict to a VPC endpoint for production, see THREAT_MODEL.md)
  data access policy <name>-data  aoss:* on the collection and its indexes for: the pipeline role, the evaluation
                                  identity and the OpenSearch UI operators you pass with --principals
  collection        <name>-ts     TIMESERIES, in the group

Usage:
  python deploy/01_collection.py --name agentobs --region us-east-1 --profile default \
      --principals arn:aws:iam::<account-id>:role/Admin,arn:aws:iam::<account-id>:user/<you>
Prints the collection endpoint and ARN when ACTIVE (a few minutes). Needs: aoss:* and iam:GetRole.
"""
import argparse
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

TAG_PROJECT = "agentic-observability-serverless"


def code(e):
    return getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="agentobs", help="prefix for every resource name")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--principals", required=True, help="comma-separated IAM ARNs that get data access (operators, evaluation identity)")
    ap.add_argument("--max-ocu", type=int, default=2, help="max indexing and search OCU for the group")
    ap.add_argument("--min-search-ocu", type=int, default=0, help="0 scales search to zero when idle; 2 keeps search capacity on for production/mission-critical/always-on workloads. Allowed: 0, 2, 4, 8, 16 or multiples of 16")
    ap.add_argument("--tags", default="", help="extra tags k=v,k=v")
    a = ap.parse_args()
    if a.min_search_ocu not in (0, 2, 4, 8) and a.min_search_ocu % 16 != 0:
        print("--min-search-ocu must be 0, 2, 4, 8, 16 or a multiple of 16 (service limit)")
        return 2
    if a.max_ocu < max(a.min_search_ocu, 2):
        print("--max-ocu must be at least 2 and not below --min-search-ocu")
        return 2

    s = boto3.Session(profile_name=a.profile, region_name=a.region)
    acct = s.client("sts").get_caller_identity()["Account"]
    aoss = s.client("opensearchserverless")
    grp, col = a.name, f"{a.name}-ts"
    pipeline_role = f"arn:aws:iam::{acct}:role/{a.name}-pipeline-role"
    tags = [{"key": "project", "value": TAG_PROJECT}] + [
        {"key": kv.split("=", 1)[0], "value": kv.split("=", 1)[1]} for kv in a.tags.split(",") if "=" in kv]

    # 1. collection group
    if any(g["name"] == grp for g in aoss.list_collection_groups().get("collectionGroupSummaries", [])):
        print(f"collection group {grp}: exists")
    else:
        aoss.create_collection_group(
            name=grp, generation="NEXTGEN", standbyReplicas="ENABLED", tags=tags,
            description="agentic observability sample",
            capacityLimits={"minIndexingCapacityInOCU": 0, "maxIndexingCapacityInOCU": a.max_ocu,
                            "minSearchCapacityInOCU": a.min_search_ocu, "maxSearchCapacityInOCU": a.max_ocu})
        print(f"collection group {grp}: created (NEXTGEN, search {a.min_search_ocu}-{a.max_ocu} OCU, indexing 0-{a.max_ocu} OCU)")

    # 2. policies
    def security_policy(name, ptype, policy):
        try:
            aoss.get_security_policy(name=name, type=ptype)
            print(f"{ptype} policy {name}: exists")
        except ClientError as e:
            if code(e) != "ResourceNotFoundException":
                raise
            aoss.create_security_policy(name=name, type=ptype, policy=json.dumps(policy))
            print(f"{ptype} policy {name}: created")

    security_policy(f"{a.name}-enc", "encryption",
                    {"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{col}"]}], "AWSOwnedKey": True})
    security_policy(f"{a.name}-net", "network",
                    [{"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{col}"]},
                                {"ResourceType": "dashboard", "Resource": [f"collection/{col}"]}],
                      "AllowFromPublic": True}])

    principals = sorted({p.strip() for p in a.principals.split(",") if p.strip()} | {pipeline_role})
    data_policy = [{
        "Description": "agentic observability sample: operators, evaluation identity, ingestion pipeline role",
        "Rules": [{"ResourceType": "index", "Resource": [f"index/{col}/*"], "Permission": ["aoss:*"]},
                  {"ResourceType": "collection", "Resource": [f"collection/{col}"], "Permission": ["aoss:*"]}],
        "Principal": principals,
    }]
    pname = f"{a.name}-data"
    if any(p["name"] == pname for p in aoss.list_access_policies(type="data").get("accessPolicySummaries", [])):
        print(f"data access policy {pname}: exists (edit in the console to add principals)")
    else:
        aoss.create_access_policy(name=pname, type="data", policy=json.dumps(data_policy))
        print(f"data access policy {pname}: created for {len(principals)} principals")

    # 3. collection
    if aoss.batch_get_collection(names=[col]).get("collectionDetails"):
        print(f"collection {col}: exists")
    else:
        aoss.create_collection(name=col, type="TIMESERIES", collectionGroupName=grp, tags=tags,
                               description="agentic observability sample: OTel traces, logs, evaluation results")
        print(f"collection {col}: create requested")

    for _ in range(90):
        d = aoss.batch_get_collection(names=[col]).get("collectionDetails", [{}])[0]
        if d.get("status") == "ACTIVE" and d.get("collectionEndpoint"):
            print(f"\ncollection {col}: ACTIVE")
            print(f"COLLECTION_ENDPOINT={d['collectionEndpoint']}")
            print(f"COLLECTION_ARN={d['arn']}")
            print(f"COLLECTION_ID={d['id']}")
            return 0
        if d.get("status") == "FAILED":
            print(f"collection {col}: FAILED {d}")
            return 1
        time.sleep(10)
    print("collection not ACTIVE yet; re-run to continue waiting")
    return 1


if __name__ == "__main__":
    sys.exit(main())
