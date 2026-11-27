"""Offline tests for the sample. Run from the sample folder: python -m pytest tests/
No AWS calls: they check the files in the repository and the behaviour of scripts/render.py."""
import json, os, re, shutil, subprocess, sys, tempfile
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def read(rel): return open(os.path.join(ROOT, rel), encoding="utf-8").read()
def tracked_files():
    out = []
    for dirpath, dirnames, files in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", "rendered", "overlay", "__pycache__", ".pytest_cache", "test-notes")]
        for f in files:
            if f.endswith((".pyc", ".tgz")) or f in ("config.env", "TEST-REPORT.md"): continue
            out.append(os.path.relpath(os.path.join(dirpath, f), ROOT))
    return out
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")

# ---- render.py ---------------------------------------------------------------------------------------------------------
def make_env(tmp, **overrides):
    cfg = {"REGION": "us-east-1", "ACCOUNT_ID": "111122223333", "DOMAIN_NAME": "d", "DOMAIN_ENDPOINT": "search-d.us-east-1.es.amazonaws.com",
           "PIPELINE_NAME": "p", "INGEST_ENDPOINT": "p.us-east-1.osis.amazonaws.com", "PIPELINE_ROLE": "pr", "HOST_ROLE": "hr",
           "DLQ_BUCKET": "dlq", "CONFIG_BUCKET": "cfg", "ADMIN_ARN": "arn:aws:iam::111122223333:role/Admin", "LLM_API_KEY": "sk-x",
           "GP_DOMAIN_ENDPOINT": "search-g.us-east-1.es.amazonaws.com", "PROJECT_TAG": "t", "MODEL_PREFIX": "us"}
    cfg.update(overrides)
    for src in ("templates", "pipeline", "iam", "collector", "demo", "scripts"):
        shutil.copytree(os.path.join(ROOT, src), os.path.join(tmp, src))
    with open(os.path.join(tmp, "config.env"), "w") as f:
        f.write("\n".join(f"{k}={v}" for k, v in cfg.items() if v is not None) + "\n")
    return cfg
def run_render(tmp):
    return subprocess.run([sys.executable, os.path.join(tmp, "scripts", "render.py")], capture_output=True, text=True)

def test_render_fills_every_placeholder():
    with tempfile.TemporaryDirectory() as tmp:
        make_env(tmp)
        r = run_render(tmp)
        assert r.returncode == 0, r.stdout + r.stderr
        for dirpath, _, files in os.walk(os.path.join(tmp, "rendered")):
            for f in files:
                text = open(os.path.join(dirpath, f), encoding="utf-8").read()
                assert not PLACEHOLDER.search(text), f"unfilled placeholder in {f}"
        pipe = open(os.path.join(tmp, "rendered", "pipeline", "pipeline.yaml"), encoding="utf-8").read()
        assert "search-d.us-east-1.es.amazonaws.com" in pipe and "arn:aws:iam::111122223333:role/pr" in pipe

def test_render_fails_on_missing_key():
    with tempfile.TemporaryDirectory() as tmp:
        make_env(tmp, PROJECT_TAG=None)   # key absent entirely
        # PROJECT_TAG is not used in rendered files; remove a key that is
        with open(os.path.join(tmp, "config.env"), encoding="utf-8") as f: lines = [l for l in f if not l.startswith("DLQ_BUCKET=")]
        with open(os.path.join(tmp, "config.env"), "w", encoding="utf-8") as f: f.writelines(lines)
        r = run_render(tmp)
        assert r.returncode == 1 and "UNFILLED" in r.stdout and "DLQ_BUCKET" in r.stdout

def test_render_accepts_blank_endpoint_before_it_is_known():
    with tempfile.TemporaryDirectory() as tmp:
        make_env(tmp, INGEST_ENDPOINT="")
        r = run_render(tmp)
        assert r.returncode == 0, r.stdout + r.stderr
def test_render_drops_trailing_comments_from_values():
    with tempfile.TemporaryDirectory() as tmp:
        make_env(tmp, INGEST_ENDPOINT="   # leave blank; filled after step 3", DLQ_BUCKET="dlq-x   # a note")
        r = run_render(tmp)
        assert r.returncode == 0, r.stdout + r.stderr
        col = open(os.path.join(tmp, "rendered", "collector", "otelcol-config-extras.yml"), encoding="utf-8").read()
        assert "leave blank" not in col and "https:///p/v1/traces" in col
        pipe = open(os.path.join(tmp, "rendered", "pipeline", "pipeline.yaml"), encoding="utf-8").read()
        assert '"dlq-x"' in pipe and "a note" not in pipe
def test_config_example_keeps_comments_on_their_own_lines():
    for line in read("config.env.example").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            assert "#" not in line, f"trailing comment: {line}"
def test_model_prefix_sets_the_bedrock_profiles():
    assert "us.anthropic." not in read("demo/litellm/config.yaml") and "us.anthropic." not in read("iam/host-role-policy.json")
    with tempfile.TemporaryDirectory() as tmp:
        make_env(tmp, REGION="ap-southeast-2", MODEL_PREFIX="au")
        assert run_render(tmp).returncode == 0
        lit = open(os.path.join(tmp, "rendered", "demo", "litellm", "config.yaml"), encoding="utf-8").read()
        pol = open(os.path.join(tmp, "rendered", "iam", "host-role-policy.json"), encoding="utf-8").read()
        assert lit.count("bedrock/converse/au.anthropic.") == 4 and "us.anthropic." not in lit
        assert "inference-profile/au.anthropic.claude-haiku-5-5" in pol and "inference-profile/au.anthropic.claude-sonnet-5-5" in pol

# ---- templates ---------------------------------------------------------------------------------------------------------
def leaf(props, dotted):
    node = {"properties": props}
    for part in dotted.split("."):
        node = node.get("properties", {}).get(part)
        if node is None: return None
    return node
SPANS = json.loads(read("templates/otel-spans.json")); LOGS = json.loads(read("templates/otel-logs.json"))
SPANS_T = json.loads(read("templates/tuned/otel-spans-tuned.json")); LOGS_T = json.loads(read("templates/tuned/otel-logs-tuned.json"))

def test_templates_cover_the_pipeline_index_names():
    assert SPANS["index_patterns"] == ["otel-v2-apm-span-*"] and LOGS["index_patterns"] == ["logs-otel-v1-*"]
    assert SPANS_T["index_patterns"] == ["otel-v2-apm-span-*"] and LOGS_T["index_patterns"] == ["logs-otel-v1-*"]

def test_every_attribute_bag_value_is_keyword():
    for t in (SPANS, LOGS, SPANS_T, LOGS_T):
        rules = [list(r.values())[0] for r in t["template"]["mappings"]["dynamic_templates"]]
        bags = {"attributes.*", "resource.attributes.*", "instrumentationScope.attributes.*"}
        if t is SPANS or t is SPANS_T: bags |= {"events.attributes.*", "links.attributes.*"}
        for bag in bags:
            types = {r["match_mapping_type"] for r in rules if r.get("path_match") == bag}
            assert types == {"string", "long", "double", "boolean"}, f"{bag}: {types}"
            assert all(r["mapping"]["type"] == "keyword" for r in rules if r.get("path_match") == bag)
        assert not any(r.get("match_mapping_type") == "*" for r in rules), "a '*' rule maps intermediate objects as keyword"

def test_fields_that_are_aggregated_or_read_by_agent_traces_are_typed():
    p = SPANS["template"]["mappings"]["properties"]
    assert leaf(p, "durationInNanos")["type"] == "long" and leaf(p, "startTime")["type"] == "date_nanos"
    assert leaf(p, "gen_ai.usage.input_tokens")["type"] == "long" and leaf(p, "gen_ai.usage.output_tokens")["type"] == "long"
    assert leaf(p, "attributes.derived.gen_ai.is_root")["type"] == "boolean"
    assert leaf(p, "attributes.gen_ai.usage.input_tokens")["type"] == "long"
    assert leaf(p, "attributes.gen_ai.input.messages")["ignore_above"] >= 32766

def test_only_searchable_fields_are_text():
    def texts(props, prefix=""):
        for k, v in props.items():
            if "properties" in v: yield from texts(v["properties"], prefix + k + ".")
            elif v.get("type") == "text": yield prefix + k
    assert set(texts(SPANS["template"]["mappings"]["properties"])) == {"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result"}
    assert set(texts(LOGS["template"]["mappings"]["properties"])) == {"body"}

def test_events_and_links_are_nested_and_tuned_adds_index_false():
    p = SPANS["template"]["mappings"]["properties"]
    assert p["events"]["type"] == "nested" and p["links"]["type"] == "nested"
    pt = SPANS_T["template"]["mappings"]["properties"]
    assert pt["serviceName"].get("index") is False and pt["serviceName"].get("low_cardinality") is True
    assert SPANS_T["template"]["settings"]["index.parquet.bloom_filter_enabled.field"] == ["traceId"]

# ---- pipeline ------------------------------------------------------------------------------------------------------------
def load_pipeline(name):
    text = PLACEHOLDER.sub("x", read(f"pipeline/{name}"))
    return yaml.safe_load(text)
def sinks(p):
    for sub, body in p.items():
        if sub == "version": continue
        for s in body.get("sink", []):
            if "opensearch" in s: yield sub, s["opensearch"]

def test_pipeline_parses_and_every_sink_has_a_dead_letter_queue():
    for name in ("pipeline.yaml", "pipeline-storage-comparison.yaml"):
        p = load_pipeline(name)
        found = list(sinks(p))
        assert found, name
        for sub, s in found:
            assert "dlq" in s and "s3" in s["dlq"], f"{name}/{sub}: sink without DLQ"
            assert s["aws"]["serverless"] is False

def test_pipeline_routes_logs_and_traces_to_the_template_index_names():
    p = load_pipeline("pipeline.yaml")
    idx = {s.get("index") or s.get("index_type") for _, s in sinks(p)}
    assert idx == {"logs-otel-v1-%{yyyy.MM.dd}", "otel-v2-apm-span-%{yyyy.MM.dd}", "otel-v2-apm-service-map"}
    assert p["otlp-pipeline"]["source"]["otlp"]["traces_path"] == "/x/v1/traces"

def test_pipeline_copies_genai_fields_to_the_root_and_marks_the_agent_root():
    p = load_pipeline("pipeline.yaml")
    procs = p["otel-traces-pipeline"]["processor"]
    copies = [e["to_key"] for pr in procs if "copy_values" in pr for e in pr["copy_values"]["entries"]]
    for f in ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.tool.call.result", "gen_ai.operation.name"):
        assert f in copies, f
    adds = [e for pr in procs if "add_entries" in pr for e in pr["add_entries"]["entries"]]
    assert {e["key"] for e in adds} == {"parentSpanId", "gen_ai.operation.name", "attributes/gen_ai.operation.name"}
    assert all(e["add_when"].startswith('/genai_op == "invoke_workflow"') for e in adds)

def test_storage_comparison_pipeline_writes_each_signal_to_both_domains():
    p = load_pipeline("pipeline-storage-comparison.yaml")
    for sub in ("otel-logs-pipeline", "otel-traces-pipeline", "service-map-pipeline"):
        hosts = [s["hosts"][0] for s2, s in sinks(p) if s2 == sub]
        assert len(hosts) == 2, f"{sub}: {hosts}"

# ---- collector, user-data, IAM ---------------------------------------------------------------------------------------------
def test_collector_override_targets_the_pipeline_path_with_sigv4():
    c = yaml.safe_load(PLACEHOLDER.sub("x", read("collector/otelcol-config-extras.yml")))
    assert c["extensions"]["sigv4auth"]["service"] == "osis"
    for name in ("otlp_http/osis-logs", "otlp_http/osis-traces"):
        ex = c["exporters"][name]
        url = ex.get("logs_endpoint") or ex.get("traces_endpoint")
        assert url.startswith("https://x/x/v1/"), url
        assert ex["auth"]["authenticator"] == "sigv4auth"
    assert "otlp_http/osis-traces" in c["service"]["pipelines"]["traces"]["exporters"]
    assert "otlp_http/osis-logs" in c["service"]["pipelines"]["logs"]["exporters"]

def test_user_data_fails_fast_pins_the_demo_and_protects_secrets():
    u = read("demo/user-data.sh")
    assert "set -euxo pipefail" in u
    assert "git checkout 3.1.0" in u
    assert "chmod 600" in u and "litellm.env" in u
    assert "{{{{" not in u
    # the upstream 3.1.0 health checks work; an override with bash broke recommendation (no bash in the image)
    assert "compose.extras.yaml" not in u
    assert len(u.encode()) < 16 * 1024

def test_demo_pins_images():
    env = read("demo/.env.override")
    assert re.search(r"^DEMO_VERSION=3\.1\.0$", env, re.M), "DEMO_VERSION must pin the image tag, not latest"
    compose = yaml.safe_load(read("demo/compose.bedrock.yaml"))
    assert "@sha256:" in compose["services"]["litellm"]["image"]

def test_host_role_is_least_privilege():
    pol = json.loads(read("iam/host-role-policy.json"))
    actions = {a for s in pol["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}
    assert actions == {"osis:Ingest", "osis:GetPipeline", "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "s3:GetObject"}
    s3 = [s for s in pol["Statement"] if s["Action"] == "s3:GetObject"][0]
    assert s3["Resource"].endswith("/demo-overlay.tgz")

def test_all_access_mapping_keeps_the_admin_in_both_lists():
    m = json.loads(read("iam/all-access-rolesmapping.json"))
    assert "{{ADMIN_ARN}}" in m["backend_roles"] and "{{ADMIN_ARN}}" in m["users"]

# ---- scrub -----------------------------------------------------------------------------------------------------------------
def test_no_account_ids_endpoints_or_keys_in_the_repository():
    bad = re.compile(r"\b\d{12}\b(?<!111122223333)|search-[a-z0-9-]+-[a-z0-9]{26}\.|[a-z0-9-]+-[a-z0-9]{26}\.us-[a-z]+-\d\.osis|sk-[0-9a-f]{32,}|\bi-0[0-9a-f]{16}\b|AKIA[0-9A-Z]{16}")
    for f in tracked_files():
        text = open(os.path.join(ROOT, f), encoding="utf-8", errors="ignore").read()
        m = bad.search(text)
        assert not m, f"{f}: {m.group(0)}"

def test_no_event_or_environment_framing():
    words = re.compile(r"re:Invent|kiosk|booth|demo video|our run|cost us|blog post", re.I)
    for f in tracked_files():
        if f.startswith("tests"): continue
        text = open(os.path.join(ROOT, f), encoding="utf-8", errors="ignore").read()
        m = words.search(text)
        assert not m, f"{f}: {m.group(0)}"

def test_shell_scripts_avoid_gnu_only_date_flags():
    # macOS ships BSD date: no -d / --date
    for f in tracked_files():
        if f.endswith(".sh") and not f.startswith("demo/"):   # demo/user-data.sh runs on Amazon Linux
            text = open(os.path.join(ROOT, f), encoding="utf-8").read()
            assert not re.search(r"\bdate\b[^\n|;]*\s(-d|--date)\b", text), f"{f}: GNU-only date flag"

def test_readme_runs_python_through_a_detected_interpreter():
    r = read("README.md")
    assert "$PY scripts/render.py" in r and "python scripts/render.py" not in r

def load_setup_ui():
    import importlib.util
    spec = importlib.util.spec_from_file_location("setup_ui", os.path.join(ROOT, "scripts", "setup_ui.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_console_identity_goes_to_the_right_all_access_list():
    m = load_setup_ui()
    assert m.mapping_entry("arn:aws:sts::111122223333:assumed-role/Admin/jane-session") == ("backend_roles", "arn:aws:iam::111122223333:role/Admin")
    assert m.mapping_entry("arn:aws:iam::111122223333:role/Admin") == ("backend_roles", "arn:aws:iam::111122223333:role/Admin")
    assert m.mapping_entry("arn:aws:iam::111122223333:user/jane") == ("users", "arn:aws:iam::111122223333:user/jane")

def test_ui_admins_always_include_the_identity_running_the_script():
    m = load_setup_ui()
    console, cli = "arn:aws:sts::111122223333:assumed-role/Admin/jane", "arn:aws:iam::111122223333:user/ci"
    assert m.app_admins([console], cli) == [console, cli]
    assert m.app_admins([cli], cli) == [cli]
    src = read("scripts/setup_ui.py")
    assert "json.dumps(ui_admins)" in src and "have + ui_admins" in src and "a.workspace, ui_admins" in src

def test_readme_checks_names_before_creating_and_strips_carriage_returns():
    r = read("README.md")
    for s in ("exists: bucket $DLQ_BUCKET", "exists: role $PIPELINE_ROLE", "exists: domain $DOMAIN_NAME", "exists: pipeline $PIPELINE_NAME"):
        assert s in r
    assert r.index("exists: bucket") < r.index("1. **Domain.**")
    for line in r.splitlines():
        if re.search(r"=\$\(aws ", line) and "--output text" in line:
            assert "tr -d '\\r'" in line, f"captured CLI value without tr -d: {line.strip()[:80]}"
    assert "--messages file://msg.json" in r and "--messages '[" not in r

def test_readme_uses_setup_ui_and_picked_instance_types():
    r = read("README.md")
    assert "$PY scripts/setup_ui.py --admins" in r
    assert "InstanceType=$OS_TYPE" in r and "--instance-type $EC2_TYPE" in r
    assert "InstanceType=or2.large.search" not in r and "--instance-type m6i.2xlarge" not in r
    assert "boto3" in read("requirements.txt")
