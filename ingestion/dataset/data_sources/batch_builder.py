# SPDX-License-Identifier: MIT-0
"""
OpenSearch batch builder for formatting data.
"""
import json
from typing import List, Dict, Any


class BatchBuilder:
    """Builds batches of documents for processing."""
    
    @staticmethod
    def build_bulk_batch(records: List[Dict[str, Any]], index_name: str) -> str:
        """Convert records to JSONL format (one document per line)."""
        lines = []
        
        for record in records:
            lines.append(json.dumps(record))
        
        return '\n'.join(lines) + '\n'
