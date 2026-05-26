#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""
Setup Test Environment for OpenSearch Index Comparison

Creates two indices in a *local* OpenSearch cluster and seeds the target with a
configurable percentage of documents missing relative to the source. The set of
expected-missing IDs is written to `expected_missing_ids.txt` for verification.

This harness assumes a no-auth, plaintext local cluster (e.g. the one in
docker-compose.yml). Do not point it at a production endpoint.
"""

import argparse
import logging
import random
import string
import sys
import time

from opensearchpy import OpenSearch, RequestsHttpConnection, helpers
from opensearchpy.exceptions import ConnectionError as OSConnectionError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Setup test environment for OpenSearch index comparison."
    )
    parser.add_argument("--host", default="localhost", help="OpenSearch host (default: localhost)")
    parser.add_argument("--port", type=int, default=9200, help="OpenSearch port (default: 9200)")
    parser.add_argument(
        "--source-index",
        default="test_source",
        help="Source index name (default: test_source)",
    )
    parser.add_argument(
        "--target-index",
        default="test_target",
        help="Target index name (default: test_target)",
    )
    parser.add_argument(
        "--doc-count",
        type=int,
        default=1000,
        help="Number of documents to create (default: 1000)",
    )
    parser.add_argument(
        "--missing-percentage",
        type=int,
        default=20,
        help="Percentage of documents to exclude from target index (default: 20)",
    )
    return parser.parse_args()


def create_opensearch_client(host, port):
    client_kwargs = {
        "hosts": [{"host": host, "port": port}],
        "connection_class": RequestsHttpConnection,
        "use_ssl": False,
        "verify_certs": False,
        "ssl_show_warn": False,
        "timeout": 30,
    }
    try:
        client = OpenSearch(**client_kwargs)
        client.info()
        logger.info("Connected to OpenSearch at %s:%s", host, port)
        return client
    except OSConnectionError as e:
        logger.error("Failed to connect to OpenSearch: %s", e)
        logger.error("Make sure OpenSearch is running and accessible.")
        sys.exit(1)


def create_index(client, index_name):
    if client.indices.exists(index=index_name):
        logger.info("Index '%s' already exists. Deleting it...", index_name)
        client.indices.delete(index=index_name)

    index_body = {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {
            "properties": {
                "title": {"type": "text"},
                "content": {"type": "text"},
                "timestamp": {"type": "date"},
                "tags": {"type": "keyword"},
            }
        },
    }
    client.indices.create(index=index_name, body=index_body)
    logger.info("Created index '%s'", index_name)


def generate_random_document():
    return {
        "title": "Title " + "".join(random.choices(string.ascii_uppercase, k=10)),
        "content": "Content " + "".join(random.choices(string.ascii_letters, k=50)),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tags": random.sample(["tag1", "tag2", "tag3", "tag4", "tag5"], random.randint(1, 3)),
    }


def bulk_index_documents(client, index_name, doc_ids):
    actions = [
        {"_index": index_name, "_id": doc_id, "_source": generate_random_document()}
        for doc_id in doc_ids
    ]
    success, failed = helpers.bulk(client, actions, stats_only=True)
    logger.info("Indexed %d documents to '%s', %d failed", success, index_name, failed)
    if failed:
        logger.error("Bulk index had %d failures; aborting test setup", failed)
        sys.exit(1)
    client.indices.refresh(index=index_name)
    logger.info("Refreshed index '%s'", index_name)


def main():
    args = parse_arguments()

    client = create_opensearch_client(args.host, args.port)
    create_index(client, args.source_index)
    create_index(client, args.target_index)

    all_doc_ids = [f"doc_{i}" for i in range(1, args.doc_count + 1)]
    missing_count = int(args.doc_count * args.missing_percentage / 100)
    missing_ids = set(random.sample(all_doc_ids, missing_count))
    target_doc_ids = [doc_id for doc_id in all_doc_ids if doc_id not in missing_ids]

    logger.info(
        "Indexing %d documents to source index '%s'", len(all_doc_ids), args.source_index
    )
    bulk_index_documents(client, args.source_index, all_doc_ids)

    logger.info(
        "Indexing %d documents to target index '%s'", len(target_doc_ids), args.target_index
    )
    bulk_index_documents(client, args.target_index, target_doc_ids)

    missing_ids_file = "expected_missing_ids.txt"
    with open(missing_ids_file, "w") as f:
        for doc_id in sorted(missing_ids):
            f.write(f"{doc_id}\n")

    logger.info("Wrote %d expected missing IDs to '%s'", len(missing_ids), missing_ids_file)
    logger.info(
        "Setup complete. Source: '%s', Target: '%s'. Expected missing: %d (%d%%).",
        args.source_index,
        args.target_index,
        len(missing_ids),
        args.missing_percentage,
    )
    logger.info(
        "Run comparison with: python compare_indices.py --auth-mode none "
        "--source %s --target %s --output missing_ids.txt --port %d",
        args.source_index,
        args.target_index,
        args.port,
    )


if __name__ == "__main__":
    main()
