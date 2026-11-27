"""Step 4: OpenSearch UI application, Ask AI, and an empty observability workspace. Datasets stay a console step (04_ui.md).

Creates (idempotent):
  OpenSearch UI application  <name>-ui   data source = the <name>-ts collection, dashboard admins = --admins plus the
                                         deploying identity (so this script can create the workspace), tagged
                                         project=agentic-observability-serverless
  Ask AI / Agentic Chat enabled on the application (free; off by default on a new app; skipped with a note where the
                                         region does not offer it)
  Workspace                  "<--workspace>"  Observability use case, the collection attached, write access granted to
                                         --admins so you can edit it in the console. You create the datasets there
                                         (the UI fills in their field lists; a script cannot).

Usage:
  python deploy/04_ui.py --name agentobs --region us-east-1 --profile default --admins arn:aws:sts::<account-id>:assumed-role/<Role>/<session>,arn:aws:iam::<account-id>:user/<you>
--admins takes the identity exactly as you are signed in to the AWS console: the user ARN for an IAM user, the
session ARN (arn:aws:sts::...:assumed-role/...) for a role. Default: the identity running this script.
Prints the application endpoint when ACTIVE (usually a few minutes).
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
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


ADMIN_KEY = "opensearchDashboards.dashboardAdmin.users"


def ensure_app_admins(osc, app_id, detail, wanted):
    """The identity running this script must be an app admin, or it cannot see the data source to build the
    workspace. Merge `wanted` into the app's admin list (self-heals a re-run of an app created without it)."""
    cfg = {c["key"]: c["value"] for c in detail.get("appConfigs", [])}
    current = json.loads(cfg.get(ADMIN_KEY, "[]"))
    merged = list(dict.fromkeys(current + wanted))
    if set(merged) != set(current):
        osc.update_application(id=app_id, appConfigs=[{"key": ADMIN_KEY, "value": json.dumps(merged)}])
        print(f"UI application: admins now {', '.join(merged)}")


def ui_call(session, region, host, method, path, body=None, timeout=60):
    """Signed call to the OpenSearch UI application's Dashboards API (service 'opensearch')."""
    url = f"https://{host}{path}"
    data = json.dumps(body).encode() if body is not None else b""
    headers = {"osd-xsrf": "true", "x-amz-content-sha256": hashlib.sha256(data).hexdigest()}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = AWSRequest(method=method, url=url, data=data if body is not None else None, headers=headers)
    SigV4Auth(session.get_credentials(), "opensearch", region).add_auth(req)
    r = urllib.request.Request(url, data=data if body is not None else None, headers=dict(req.headers), method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - best effort; the console fallback covers failures
        return None, f"{type(e).__name__}: {e}"


def create_workspace(session, region, host, ws_name, principals):
    """Create an empty Observability workspace with the collection attached, editable by the console admins.
    Datasets are left to the user (the UI populates their field lists; the API does not)."""
    # the collection's data-source saved object appears once the app is active; wait briefly for it
    ds_id = None
    for _ in range(8):
        st, body = ui_call(session, region, host, "GET", "/api/saved_objects/_find?type=data-source&per_page=50")
        srcs = json.loads(body).get("saved_objects", []) if st == 200 else []
        if srcs:
            ds_id = srcs[0]["id"]
            break
        time.sleep(15)
    if not ds_id:
        print("Workspace: data source not ready yet; create the workspace in the console (deploy/04_ui.md)")
        return

    st, body = ui_call(session, region, host, "POST", "/api/workspaces/_list", {"perPage": 100})
    existing = next((w for w in (json.loads(body).get("result", {}).get("workspaces", []) if st == 200 else [])
                     if w.get("name") == ws_name), None)
    if existing:
        print(f"Workspace: \"{ws_name}\" exists ({existing['id']})")
        return

    users = list(dict.fromkeys(principals))
    perms = {k: {"users": users} for k in ("read", "write", "library_read", "library_write")}
    st, body = ui_call(session, region, host, "POST", "/api/workspaces", {
        "attributes": {"name": ws_name, "description": "agentic observability sample",
                       "features": ["use-case-observability"], "color": "#54B399"},
        "settings": {"dataSources": [ds_id], "permissions": perms}})
    ok = st == 200 and json.loads(body).get("success")
    if ok:
        print(f"Workspace: created \"{ws_name}\" ({json.loads(body)['result']['id']}), editable by {', '.join(users)}")
    else:
        print(f"Workspace: not created ({st}); create it in the console (deploy/04_ui.md). {body[:160]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="agentobs")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--admins", default=None, help="comma-separated ARNs as signed in to the console (session ARN for a role)")
    ap.add_argument("--workspace", default="Astronomy Shop", help="name of the observability workspace to create")
    a = ap.parse_args()

    s = boto3.Session(profile_name=a.profile, region_name=a.region)
    caller = s.client("sts").get_caller_identity()["Arn"]
    admins = list(dict.fromkeys(x.strip() for x in (a.admins or caller).split(",") if x.strip()))
    # the deploying identity must be an app admin too, or it cannot see the data source to create the workspace
    app_admins = list(dict.fromkeys([*admins, caller]))
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
            appConfigs=[{"key": ADMIN_KEY, "value": json.dumps(app_admins)}],
            tagList=[{"Key": "project", "Value": TAG_PROJECT}])
        print(f"UI application {app_name}: create requested ({app['id']}), admins: {', '.join(app_admins)}")

    for _ in range(90):  # up to 15 min
        d = osc.get_application(id=app["id"])
        if d.get("status") == "ACTIVE" and d.get("endpoint"):
            host = d["endpoint"].removeprefix("https://")
            print(f"\nUI application {app_name}: ACTIVE")
            enable_ask_ai(osc, app["id"])
            ensure_app_admins(osc, app["id"], d, app_admins)
            create_workspace(s, a.region, host, a.workspace, app_admins)
            print(f"UI_ENDPOINT=https://{host}")
            print(f"Open it signed in to the AWS console as one of the admins, choose the \"{a.workspace}\" workspace, "
                  f"then create the datasets as in deploy/04_ui.md.")
            return 0
        if d.get("status") == "FAILED":
            print(f"UI application {app_name}: FAILED")
            return 1
        time.sleep(10)
    print("UI application not ACTIVE yet; re-run to continue waiting")
    return 1


if __name__ == "__main__":
    sys.exit(main())
