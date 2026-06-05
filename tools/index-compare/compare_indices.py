#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""
OpenSearch Index Comparison Tool

Compares two indices and writes document IDs that exist in the source but are
missing from the target.

Supports four connection modes for Amazon OpenSearch Service and local clusters:
  - aws-iam: SigV4-signed requests against an AOS managed domain (service='es')
            or OpenSearch Serverless (service='aoss'), using the boto3 default
            credential chain. Override the service with --aws-service.
  - basic:   HTTP basic auth (fine-grained access control or local with security).
  - none:    No auth (local docker-compose dev cluster).

Configuration precedence (highest first):
  1. CLI flags
  2. Process environment variables
  3. .env file (loaded via python-dotenv if present)
  4. Built-in defaults

Note on pagination: this sample uses the Scroll API for compatibility with
older OpenSearch versions. For new code on OpenSearch 2.4+ (managed) consider
Point-in-Time + search_after, which avoids server-side scroll context state.

See .env.example for the supported variables.
"""

import argparse
import logging
import os
import sys
import time
from getpass import getpass
from typing import Optional

from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.exceptions import (
    AuthenticationException,
    ConnectionError as OSConnectionError,
    NotFoundError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


AUTH_MODES = ("aws-iam", "basic", "none")
AWS_SERVICES = ("es", "aoss")
TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}


def load_dotenv_if_present(path: Optional[str] = None) -> None:
    """Load a .env file into os.environ without overriding existing vars."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(dotenv_path=path, override=False)


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def parse_bool_env(name: str) -> Optional[bool]:
    raw = env(name)
    if raw is None:
        return None
    norm = raw.strip().lower()
    if norm in TRUTHY:
        return True
    if norm in FALSY:
        return False
    logger.error(
        "Invalid boolean for %s: %r. Use one of: %s (truthy) or %s (falsy).",
        name,
        raw,
        ", ".join(sorted(TRUTHY)),
        ", ".join(sorted(FALSY)),
    )
    sys.exit(2)


def parse_int_env(name: str, default: int) -> int:
    raw = env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.error("Invalid integer for %s: %r", name, raw)
        sys.exit(2)


def is_local_host(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two OpenSearch indices and find missing document IDs.",
    )

    parser.add_argument(
        "--env-file",
        help="Path to a .env file (default: ./.env if present)",
    )

    parser.add_argument("--source", help="Source index name (env: OS_SOURCE_INDEX)")
    parser.add_argument("--target", help="Target index name (env: OS_TARGET_INDEX)")
    parser.add_argument("--output", help="Output file for missing IDs (env: OS_OUTPUT_FILE)")

    parser.add_argument(
        "--auth-mode",
        choices=AUTH_MODES,
        help="Auth mode: aws-iam | basic | none (env: OS_AUTH_MODE, default: none)",
    )
    parser.add_argument("--host", help="OpenSearch host (env: OS_HOST, default: localhost)")
    parser.add_argument("--port", type=int, help="OpenSearch port (env: OS_PORT, default: 9200)")
    parser.add_argument(
        "--use-ssl",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use HTTPS (env: OS_USE_SSL). Forced True for aws-iam and for non-local hosts.",
    )
    parser.add_argument(
        "--verify-certs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Verify TLS certs (env: OS_VERIFY_CERTS). Defaults True except for local hosts.",
    )

    parser.add_argument("--user", help="Basic auth username (env: OS_USER)")
    parser.add_argument(
        "--password",
        help=(
            "Basic auth password. WARNING: appears in process listings (ps) and shell history. "
            "Prefer OS_PASSWORD env var or --password-stdin."
        ),
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read basic auth password from stdin (recommended for scripting).",
    )

    parser.add_argument(
        "--aws-region",
        help="AWS region for SigV4 signing (env: AWS_REGION or OS_AWS_REGION)",
    )
    parser.add_argument(
        "--aws-service",
        choices=AWS_SERVICES,
        help=(
            "AWS service identifier for SigV4: 'es' for managed AOS domains (default), "
            "'aoss' for OpenSearch Serverless (env: OS_AWS_SERVICE)."
        ),
    )

    parser.add_argument(
        "--scroll-size",
        type=int,
        help="Documents per scroll batch (env: OS_SCROLL_SIZE, default: 1000)",
    )
    parser.add_argument(
        "--scroll-time",
        help="Scroll context duration (env: OS_SCROLL_TIME, default: 5m)",
    )

    return parser.parse_args()


def resolve_password(args: argparse.Namespace) -> Optional[str]:
    if args.password_stdin:
        return getpass("Password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    return args.password or env("OS_PASSWORD")


def resolve_config(args: argparse.Namespace) -> dict:
    """Layer CLI > env > defaults into a single dict."""
    cfg = {
        "source": args.source or env("OS_SOURCE_INDEX"),
        "target": args.target or env("OS_TARGET_INDEX"),
        "output": args.output or env("OS_OUTPUT_FILE"),
        "auth_mode": args.auth_mode or env("OS_AUTH_MODE", "none"),
        "host": args.host or env("OS_HOST", "localhost"),
        "port": args.port if args.port is not None else parse_int_env("OS_PORT", 9200),
        "user": args.user or env("OS_USER"),
        "password": resolve_password(args),
        "aws_region": args.aws_region or env("AWS_REGION") or env("OS_AWS_REGION"),
        "aws_service": args.aws_service or env("OS_AWS_SERVICE", "es"),
        "scroll_size": args.scroll_size
        if args.scroll_size is not None
        else parse_int_env("OS_SCROLL_SIZE", 1000),
        "scroll_time": args.scroll_time or env("OS_SCROLL_TIME", "5m"),
    }

    cfg["use_ssl"] = args.use_ssl if args.use_ssl is not None else parse_bool_env("OS_USE_SSL")
    cfg["verify_certs"] = (
        args.verify_certs if args.verify_certs is not None else parse_bool_env("OS_VERIFY_CERTS")
    )

    missing = [k for k in ("source", "target", "output") if not cfg[k]]
    if missing:
        logger.error(
            "Missing required configuration: %s. Provide via CLI flags or .env "
            "(OS_SOURCE_INDEX, OS_TARGET_INDEX, OS_OUTPUT_FILE).",
            ", ".join(missing),
        )
        sys.exit(2)

    if cfg["auth_mode"] not in AUTH_MODES:
        logger.error(
            "Invalid auth-mode '%s'. Must be one of: %s",
            cfg["auth_mode"],
            ", ".join(AUTH_MODES),
        )
        sys.exit(2)

    if cfg["aws_service"] not in AWS_SERVICES:
        logger.error(
            "Invalid aws-service '%s'. Must be one of: %s",
            cfg["aws_service"],
            ", ".join(AWS_SERVICES),
        )
        sys.exit(2)

    local = is_local_host(cfg["host"])
    if cfg["auth_mode"] == "aws-iam":
        cfg["use_ssl"] = True
        if cfg["verify_certs"] is None:
            cfg["verify_certs"] = True
    else:
        if cfg["use_ssl"] is None:
            cfg["use_ssl"] = not local
        if cfg["verify_certs"] is None:
            cfg["verify_certs"] = not local

    return cfg


def create_opensearch_client(cfg: dict) -> OpenSearch:
    client_kwargs = {
        "hosts": [{"host": cfg["host"], "port": cfg["port"]}],
        "use_ssl": cfg["use_ssl"],
        "verify_certs": cfg["verify_certs"],
        "ssl_show_warn": cfg["verify_certs"],
        "connection_class": RequestsHttpConnection,
        "timeout": 60,
    }

    mode = cfg["auth_mode"]
    if mode == "aws-iam":
        try:
            import boto3
            from opensearchpy import AWSV4SignerAuth
        except ImportError as e:
            logger.error("aws-iam mode requires boto3 and opensearch-py>=2.0: %s", e)
            sys.exit(1)

        region = cfg["aws_region"] or boto3.Session().region_name
        if not region:
            logger.error(
                "AWS region required for aws-iam mode. Set --aws-region, AWS_REGION, "
                "or configure a default region."
            )
            sys.exit(2)

        credentials = boto3.Session().get_credentials()
        if credentials is None:
            logger.error("No AWS credentials found via the boto3 default credential chain.")
            sys.exit(1)

        client_kwargs["http_auth"] = AWSV4SignerAuth(credentials, region, cfg["aws_service"])
        logger.info(
            "Using AWS SigV4 (service='%s') signing for region %s",
            cfg["aws_service"],
            region,
        )

    elif mode == "basic":
        if not (cfg["user"] and cfg["password"]):
            logger.error(
                "basic auth-mode requires --user plus --password / --password-stdin / OS_PASSWORD."
            )
            sys.exit(2)
        client_kwargs["http_auth"] = (cfg["user"], cfg["password"])
        logger.info("Using HTTP basic authentication")

    else:
        logger.info("Using no authentication (local/dev mode)")

    try:
        client = OpenSearch(**client_kwargs)
        info = client.info()
        logger.info(
            "Connected to OpenSearch at %s:%s (ssl=%s, verify_certs=%s) — server version %s",
            cfg["host"],
            cfg["port"],
            cfg["use_ssl"],
            cfg["verify_certs"],
            info.get("version", {}).get("number", "unknown"),
        )
        return client
    except OSConnectionError as e:
        logger.error("Failed to connect to OpenSearch: %s", e)
        sys.exit(1)
    except AuthenticationException as e:
        logger.error("Authentication failed: %s", e)
        sys.exit(1)


def get_all_document_ids(
    client: OpenSearch, index_name: str, scroll_size: int, scroll_time: str
) -> set:
    """Retrieve all document IDs from the index using the Scroll API."""
    if not client.indices.exists(index=index_name):
        logger.error("Index '%s' does not exist", index_name)
        sys.exit(1)

    query = {"query": {"match_all": {}}, "_source": False}

    logger.info("Fetching document IDs from index '%s'", index_name)
    start_time = time.time()

    resp = client.search(body=query, index=index_name, scroll=scroll_time, size=scroll_size)
    scroll_id = resp["_scroll_id"]

    try:
        hits = resp["hits"]["hits"]
        document_ids = {hit["_id"] for hit in hits}
        last_logged_at = 0
        log_every = max(scroll_size, 10000)
        logger.info(
            "Retrieved initial batch of %d document IDs from '%s'",
            len(document_ids),
            index_name,
        )

        while hits:
            resp = client.scroll(scroll_id=scroll_id, scroll=scroll_time)
            scroll_id = resp["_scroll_id"]
            hits = resp["hits"]["hits"]
            if not hits:
                break
            document_ids.update(hit["_id"] for hit in hits)
            if len(document_ids) - last_logged_at >= log_every:
                last_logged_at = len(document_ids)
                logger.info("Processed %d documents from '%s'", last_logged_at, index_name)
    except NotFoundError as e:
        logger.error(
            "Search failed against '%s'. The scroll context may have expired "
            "(consider increasing --scroll-time) or the index was removed mid-scroll: %s",
            index_name,
            e,
        )
        sys.exit(1)
    finally:
        try:
            client.clear_scroll(scroll_id=scroll_id)
        except Exception as cleanup_err:
            logger.warning("Failed to clear scroll context: %s", cleanup_err)

    elapsed = time.time() - start_time
    logger.info(
        "Retrieved %d document IDs from '%s' in %.2f seconds",
        len(document_ids),
        index_name,
        elapsed,
    )
    return document_ids


def write_missing_ids_to_file(missing_ids: set, output_file: str) -> None:
    try:
        with open(output_file, "w") as f:
            for doc_id in sorted(missing_ids):
                f.write(f"{doc_id}\n")
        logger.info("Wrote %d missing IDs to '%s'", len(missing_ids), output_file)
    except IOError as e:
        logger.error("Error writing to '%s': %s", output_file, e)
        sys.exit(1)


def main():
    args = parse_arguments()
    load_dotenv_if_present(args.env_file)
    cfg = resolve_config(args)

    client = create_opensearch_client(cfg)

    logger.info("Retrieving document IDs from source index...")
    source_ids = get_all_document_ids(
        client, cfg["source"], cfg["scroll_size"], cfg["scroll_time"]
    )

    logger.info("Retrieving document IDs from target index...")
    target_ids = get_all_document_ids(
        client, cfg["target"], cfg["scroll_size"], cfg["scroll_time"]
    )

    missing_ids = source_ids - target_ids
    logger.info(
        "Found %d document IDs in '%s' missing from '%s'",
        len(missing_ids),
        cfg["source"],
        cfg["target"],
    )

    write_missing_ids_to_file(missing_ids, cfg["output"])
    logger.info("Comparison completed successfully")


if __name__ == "__main__":
    main()
