# SPDX-License-Identifier: MIT-0
"""
Example file workload transformer.
"""
import csv
import io
from datetime import datetime
from typing import Any, Iterator, Dict
from dataset.transformers.base_transformer import BaseTransformer


class WorkloadTransformer(BaseTransformer):
    """Example CSV file transformer."""
    
    def transform_data(self, raw_data: str) -> list:
        """Parse CSV data and clean it."""
        lines = [line.strip() for line in raw_data.split('\n') if line.strip()]
        reader = csv.DictReader(io.StringIO('\n'.join(lines)))
        return list(reader)
    
    def process_records(self, data: list) -> Iterator[Dict[str, Any]]:
        """Convert CSV records to OpenSearch format."""
        # Import here to avoid circular imports
        from .config import CONFIG

        for record in data:
            # Add processing metadata
            record['processed_at'] = datetime.utcnow().isoformat()
            record['source_type'] = 'csv_file'

            # Add standard document ID field using base class helper
            record = self.add_document_id(record, CONFIG)
            yield record
