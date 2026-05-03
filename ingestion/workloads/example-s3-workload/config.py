# SPDX-License-Identifier: MIT-0
"""
Example S3 workload configuration.
"""

CONFIG = {
    "s3_bucket": "example-data-bucket",
    "s3_prefixes": ["data/", "input/"],
    "batch_size": 100,
    "max_documents": 1000,
    "region": "us-west-2",
    "document_id_field": "id"  # Field to use as OpenSearch document ID
}
