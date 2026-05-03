# SPDX-License-Identifier: MIT-0
"""
Example S3 workload transformer.
"""
import json
from datetime import datetime
from typing import Any, Iterator, Dict
from dataset.transformers.base_transformer import BaseTransformer


class WorkloadTransformer(BaseTransformer):
    """Example S3 JSONL data transformer."""
    
    def transform_data(self, raw_data: str) -> list:
        """Parse JSONL data and clean it."""
        lines = [line.strip() for line in raw_data.split('\n') if line.strip()]
        return [json.loads(line) for line in lines]
    
    def process_records(self, data: list) -> Iterator[Dict[str, Any]]:
        """Convert records to OpenSearch format."""
        # Import here to avoid circular imports
        from .config import CONFIG

        for record in data:
            # Add processing timestamp
            record['processed_at'] = datetime.utcnow().isoformat()
            record['source_type'] = 's3_jsonl'

            # Add standard document ID field using base class helper
            record = self.add_document_id(record, CONFIG)

            yield record
