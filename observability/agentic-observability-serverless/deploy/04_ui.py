"""Step 4: OpenSearch UI application connected to the collection. The workspace and datasets stay console steps (04_ui.md).

Creates (idempotent):
  OpenSearch UI application  <name>-ui   data source = the <name>-ts collection, dashboard admins from --admins,
                                         tagged project=agentic-observability-serverless
  Also enables the Ask AI / Agentic Chat features on the application (free; off by default on a new app; skipped with
  a note if the region does not offer them).

Usage:
  python deploy/04_ui.py --name agentobs --region us-east-1 --profile default --admins arn:aws:sts::<account-id>:assumed-role/<Role>/<session>,arn:aws:iam::<account-id>:user/<you>
--admins takes the identity exactly as you are signed in to the AWS console: the user ARN for an IAM user, the
session ARN (arn:aws:sts::...:assumed-role/...) for a role. Default: the identity running this script.
Prints the application endpoint when ACTIVE (usually a few minutes).
"""
import argparse
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

TAG_PROJECT = "agentic-observability-serverless"


def list_apps(osc):
    apps, token = [], None
    while True:
        r = osc.list_applications(**({"nextToken": token} if token else {}))
        apps += r.get("ApplicationSummaries", [])
        token = r.get("nextToken")
        if not token:
            return apps


def enable_ask_ai(osc, app_id):
    """Turn on the Ask AI / Agentic Chat features (free; not on by default for a new app). Best effort."""
    try:
        status = osc.get_capability(applicationId=app_id, capabilityName="ai-capability").get("status")
        if status in ("active", "creating", "updating"):
            print(f"Ask AI: {status}")
            return
    except ClientError:
        pass  # not registered yet
    try:
        r = osc.register_capability(applicationId=app_id, capabilityName="ai-capability", capabilityConfig={"aiConfig": {}})
        print(f"Ask AI: {r.get('status')} (ready in a couple of minutes)")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # e.g. DisabledOperationException where the agentic AI features are not offered in this region
        print(f"Ask AI: not enabled ({code}); enable it later in the console (application details -> AI features) "
              f"if your region supports it")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="agentobs")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--admins", default=None, help="comma-separated ARNs as signed in to the console (session ARN for a role)")
    a = ap.parse_args()

    s = boto3.Session(profile_name=a.profile, region_name=a.region)
    caller = s.client("sts").get_caller_identity()["Arn"]
    admins = list(dict.fromkeys(x.strip() for x in (a.admins or caller).split(",") if x.strip()))
    aoss, osc = s.client("opensearchserverless"), s.client("opensearch")
    app_name, col = f"{a.name}-ui", f"{a.name}-ts"

    details = aoss.batch_get_collection(names=[col]).get("collectionDetails", [])
    if not details:
        print(f"collection {col} not found; run deploy/01_collection.py first")
        return 2
    col_arn = details[0]["arn"]

    app = next((x for x in list_apps(osc) if x.get("name") == app_name), None)
    if app:
        print(f"UI application {app_name}: exists ({app['id']})")
    else:
        app = osc.create_application(
            name=app_name,
            dataSources=[{"dataSourceArn": col_arn, "dataSourceDescription": "agentic observability sample"}],
            appConfigs=[{"key": "opensearchDashboards.dashboardAdmin.users", "value": json.dumps(admins)}],
            tagList=[{"Key": "project", "Value": TAG_PROJECT}])
        print(f"UI application {app_name}: create requested ({app['id']}), admins: {', '.join(admins)}")

    for _ in range(90):  # up to 15 min
        d = osc.get_application(id=app["id"])
        if d.get("status") == "ACTIVE" and d.get("endpoint"):
            print(f"\nUI application {app_name}: ACTIVE")
            enable_ask_ai(osc, app["id"])
            print(f"UI_ENDPOINT=https://{d['endpoint'].removeprefix('https://')}")
            print("Open it while signed in to the AWS console as one of the admins, then follow deploy/04_ui.md "
                  "(workspace, datasets).")
            return 0
        if d.get("status") == "FAILED":
            print(f"UI application {app_name}: FAILED")
            return 1
        time.sleep(10)
    print("UI application not ACTIVE yet; re-run to continue waiting")
    return 1


if __name__ == "__main__":
    sys.exit(main())
