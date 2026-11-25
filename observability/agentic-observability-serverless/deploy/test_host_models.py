"""Offline checks that the model prefix and pinned versions reach everything the host is given."""
import importlib.util
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("host", HERE / "03_host.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def test_model_profiles_use_prefix():
    assert host.model_profiles("us") == ["us.anthropic.claude-haiku-5-5", "us.anthropic.claude-sonnet-5-5"]
    assert host.model_profiles("eu") == ["eu.anthropic.claude-haiku-5-5", "eu.anthropic.claude-sonnet-5-5"]


def test_litellm_config_rewrite_leaves_no_us_profiles():
    cfg = (HERE.parent / "demo-app" / "litellm" / "config.yaml").read_text(encoding="utf-8")
    out = cfg.replace("us-east-1", "eu-central-1").replace("us.anthropic.", "eu.anthropic.")
    assert "us.anthropic." not in out and "us-east-1" not in out
    assert out.count("eu.anthropic.") == cfg.count("us.anthropic.") > 0


def test_litellm_image_is_pinned_by_digest():
    compose = (HERE.parent / "demo-app" / "compose.bedrock.yaml").read_text(encoding="utf-8")
    image = re.search(r"^\s*image:\s*(\S+)", compose, re.M).group(1)
    assert re.fullmatch(r"ghcr\.io/berriai/litellm@sha256:[0-9a-f]{64}", image)


def test_user_data_uses_pinned_versions():
    ud = host.user_data("us-east-1", "example.osis.amazonaws.com", "agentobs", {})
    assert f"--branch {host.DEMO_TAG}" in ud
    assert f"/download/{host.COMPOSE_VERSION}/" in ud
