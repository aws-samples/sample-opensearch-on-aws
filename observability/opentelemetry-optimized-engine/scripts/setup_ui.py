"""OpenSearch UI for this sample, in one step (idempotent):
  1. adds your console identity to the domain's all_access mapping (merged with what is there)
  2. creates the OpenSearch UI application <DOMAIN_NAME>-ui on the domain, with your console identity and the identity
     running this script as admins, tagged project=<PROJECT_TAG>. An existing application of that name is reused (and
     missing admins added) only if it carries the same tag and has this domain as its data source; otherwise the script
     stops without changing anything. The script's own identity must be an admin: without it, the application hides
     the data source and step 4 cannot attach it.
  3. enables Ask AI on the application (free; skipped with a note where the Region does not offer it)
  4. creates an Observability workspace with the domain attached and edit access for the admins
Datasets stay a console step: the UI fills in their field lists, the API does not.

Usage:  $PY scripts/setup_ui.py --admins <console identity>[,<another>]
--admins is the identity exactly as you are signed in to the AWS console: the user ARN for an IAM user, the session ARN
(arn:aws:sts::<account>:assumed-role/<Role>/<session>) for a role. Default: the identity running this script.
Reads REGION, DOMAIN_NAME and PROJECT_TAG from config.env. Needs boto3 (requirements.txt)."""
import argparse, hashlib, json, os, re, sys, time, urllib.error, urllib.request
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def read_config(path):
    if not os.path.exists(path): sys.exit(f"{path} not found; copy config.env.example to config.env and fill it in")
    cfg = {}
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
    for k in ("REGION", "DOMAIN_NAME", "PROJECT_TAG"):
        if not cfg.get(k): sys.exit(f"{k} is empty in {path}")
    return cfg

def signed(session, region, service, method, url, body=None, extra=None):
    data = json.dumps(body).encode() if body is not None else b""
    headers = {"x-amz-content-sha256": hashlib.sha256(data).hexdigest(), **(extra or {})}
    if body is not None: headers["Content-Type"] = "application/json"
    req = AWSRequest(method=method, url=url, data=data if body is not None else None, headers=headers)
    SigV4Auth(session.get_credentials(), service, region).add_auth(req)
    r = urllib.request.Request(url, data=data if body is not None else None, headers=dict(req.headers), method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp: return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8", "replace")
    except Exception as e: return None, f"{type(e).__name__}: {e}"  # noqa: BLE001

def mapping_entry(arn, iam=None):
    """Console identity -> (list, value) in the security plugin's role mapping.
    A session ARN (arn:aws:sts::<acct>:assumed-role/<Role>/<session>) names the role without its IAM path, but the
    backend role the domain matches is the full role ARN, path included (IAM Identity Center roles live under
    /aws-reserved/sso.amazonaws.com/...). So the role ARN is read with iam:GetRole; without that permission the
    path-less ARN is used and a warning says to map the full ARN by hand."""
    m = re.match(r"arn:aws:sts::(\d{12}):assumed-role/([^/]+)/", arn)
    if m:
        if iam is not None:
            try: return "backend_roles", iam.get_role(RoleName=m.group(2))["Role"]["Arn"]
            except ClientError as e:
                print(f"all_access: could not read role {m.group(2)} ({e.response.get('Error', {}).get('Code', '')}); "
                      f"if it has an IAM path, map its full role ARN to all_access by hand")
        return "backend_roles", f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}"
    if ":role/" in arn: return "backend_roles", arn
    if ":user/" in arn: return "users", arn
    sys.exit(f"--admins: not an IAM user, role, or assumed-role session ARN: {arn}")

def app_is_ours(app, tags, domain_arn, project_tag):
    """Only an application this sample created for this domain may be reused (and its admin list extended)."""
    srcs = {x.get("dataSourceArn") for x in app.get("dataSources", [])}
    return domain_arn in srcs and tags.get("project") == project_tag

def map_all_access(session, region, endpoint, admins, iam=None):
    url = f"https://{endpoint}/_plugins/_security/api/rolesmapping/all_access"
    st, body = signed(session, region, "es", "GET", url)
    if st != 200: print(f"all_access: could not read the mapping ({st}); add your console role by hand"); return
    cur = json.loads(body).get("all_access", {})
    new = {k: list(cur.get(k, [])) for k in ("backend_roles", "hosts", "users")}
    added = []
    for a in admins:
        k, v = mapping_entry(a, iam)
        if v not in new[k]: new[k].append(v); added.append(v)
    if not added: print("all_access: console identity already mapped"); return
    st, body = signed(session, region, "es", "PUT", url, new)
    print(f"all_access: added {', '.join(added)}" if st == 200 else f"all_access: update failed ({st}) {body[:160]}")

def app_admins(admins, caller):
    """Application admins: the console identities plus the identity running this script (needed to see the data source)."""
    return list(dict.fromkeys(admins + [caller]))

def list_apps(osc):
    apps, token = [], None
    while True:
        r = osc.list_applications(**({"nextToken": token} if token else {}))
        apps += r.get("ApplicationSummaries", []); token = r.get("nextToken")
        if not token: return apps

def enable_ask_ai(osc, app_id):
    try:
        status = osc.get_capability(applicationId=app_id, capabilityName="ai-capability").get("status")
        if status in ("active", "creating", "updating"): print(f"Ask AI: {status}"); return
    except ClientError: pass
    try:
        r = osc.register_capability(applicationId=app_id, capabilityName="ai-capability", capabilityConfig={"aiConfig": {}})
        print(f"Ask AI: {r.get('status')} (ready in a couple of minutes)")
    except ClientError as e:
        print(f"Ask AI: not enabled ({e.response.get('Error', {}).get('Code', '')}); enable it later in the console "
              f"(application details > AI features) if your Region offers it")

def create_workspace(session, region, host, name, principals):
    ui = lambda m, p, b=None: signed(session, region, "opensearch", m, f"https://{host}{p}", b, {"osd-xsrf": "true"})
    ds_id = None
    for _ in range(8):
        st, body = ui("GET", "/api/saved_objects/_find?type=data-source&per_page=50")
        srcs = json.loads(body).get("saved_objects", []) if st == 200 else []
        if srcs: ds_id = srcs[0]["id"]; ds_name = srcs[0].get("attributes", {}).get("title") or ds_id; break
        time.sleep(15)
    if not ds_id: print("Workspace: data source not ready yet; re-run this script in a few minutes"); return
    st, body = ui("POST", "/api/workspaces/_list", {"perPage": 100})
    ws = json.loads(body).get("result", {}).get("workspaces", []) if st == 200 else []
    hit = next((w for w in ws if w.get("name") == name), None)
    if hit: print(f'Workspace: "{name}" exists ({hit["id"]})'); return
    users = list(dict.fromkeys(principals))
    perms = {k: {"users": users} for k in ("read", "write", "library_read", "library_write")}
    st, body = ui("POST", "/api/workspaces", {
        "attributes": {"name": name, "description": "OpenTelemetry on the optimized engine",
                       "features": ["use-case-observability"], "color": "#54B399"},
        "settings": {"dataSources": [ds_id], "permissions": perms}})
    if st == 200 and json.loads(body).get("success"):
        print(f'Workspace: created "{name}" ({json.loads(body)["result"]["id"]}) with data source {ds_name}, '
              f'editable by {", ".join(users)}')
    else:
        print(f"Workspace: not created ({st}); create it in the console. {body[:160]}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admins", default=None, help="comma-separated console identities (session ARN for a role)")
    ap.add_argument("--workspace", default="Astronomy Shop")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.env"))
    a = ap.parse_args()
    cfg = read_config(a.config); region = cfg["REGION"]
    s = boto3.Session(region_name=region)
    caller = s.client("sts").get_caller_identity()["Arn"]
    admins = list(dict.fromkeys(x.strip() for x in (a.admins or caller).split(",") if x.strip()))
    osc = s.client("opensearch")
    d = osc.describe_domain(DomainName=cfg["DOMAIN_NAME"])["DomainStatus"]
    if not d.get("Endpoint"): sys.exit(f"domain {cfg['DOMAIN_NAME']} has no endpoint yet; wait until it is active")
    ui_admins = app_admins(admins, caller)
    app_name = f"{cfg['DOMAIN_NAME']}-ui"
    app = next((x for x in list_apps(osc) if x.get("name") == app_name), None)
    if app:   # check ownership before changing anything
        cur = osc.get_application(id=app["id"])
        tags = {t["Key"]: t["Value"] for t in osc.list_tags(ARN=cur["arn"]).get("TagList", [])}
        if not app_is_ours(cur, tags, d["ARN"], cfg["PROJECT_TAG"]):
            sys.exit(f"UI application {app_name} ({app['id']}) exists but was not created by this sample for domain "
                     f"{cfg['DOMAIN_NAME']} (project tag {tags.get('project')!r}). Nothing was changed. This sample needs its "
                     f"own application: change DOMAIN_NAME in config.env, or delete that application if it is yours.")
    map_all_access(s, region, d["Endpoint"], admins, s.client("iam"))
    if app:
        conf = {c["key"]: c["value"] for c in cur.get("appConfigs", [])}
        try: have = json.loads(conf.get("opensearchDashboards.dashboardAdmin.users", "[]"))
        except ValueError: have = [x.strip() for x in conf.get("opensearchDashboards.dashboardAdmin.users", "").split(",") if x.strip()]
        want = list(dict.fromkeys(have + ui_admins))
        if conf.get("opensearchDashboards.dashboardAdmin.users") != json.dumps(want):   # also normalises a comma string to a JSON list
            osc.update_application(id=app["id"], appConfigs=[{"key": "opensearchDashboards.dashboardAdmin.users", "value": json.dumps(want)}])
            print(f"UI application {app_name}: exists ({app['id']}), admins now {', '.join(want)}")
        else:
            print(f"UI application {app_name}: exists ({app['id']})")
    else:
        app = osc.create_application(
            name=app_name, dataSources=[{"dataSourceArn": d["ARN"]}],
            appConfigs=[{"key": "opensearchDashboards.dashboardAdmin.users", "value": json.dumps(ui_admins)}],
            tagList=[{"Key": "project", "Value": cfg["PROJECT_TAG"]}])
        print(f"UI application {app_name}: create requested ({app['id']}), admins: {', '.join(ui_admins)}")
    for _ in range(90):
        g = osc.get_application(id=app["id"])
        if g.get("status") == "ACTIVE" and g.get("endpoint"):
            host = g["endpoint"].removeprefix("https://")
            enable_ask_ai(osc, app["id"])
            create_workspace(s, region, host, a.workspace, ui_admins)
            print(f"UI_ENDPOINT=https://{host}")
            return 0
        if g.get("status") == "FAILED": print(f"UI application {app_name}: FAILED"); return 1
        time.sleep(10)
    print("UI application not active yet; re-run this script to continue"); return 1

if __name__ == "__main__":
    sys.exit(main())
