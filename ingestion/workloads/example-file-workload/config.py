# SPDX-License-Identifier: MIT-0
"""
Example file workload configuration.
"""

CONFIG = {
    "file_patterns": ["./workloads/example-file-workload"],
    "base_paths": ["./data", "./input"],
    "batch_size": 50,
    "max_documents": 500,
    "document_id_field": "id"  # Field to use as OpenSearch document ID
}
