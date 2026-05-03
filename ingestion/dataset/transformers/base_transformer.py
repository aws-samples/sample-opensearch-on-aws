# SPDX-License-Identifier: MIT-0
"""
Base transformer interface for user implementations.
"""
from typing import Any, Iterator, Dict
from dataset.data_sources.base_data_source import BaseDataSource


class BaseTransformer(BaseDataSource):
    """Abstract base for data transformation implementations."""
    
    def add_document_id(self, record: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        """Add standard document ID field for OSIS pipeline."""
        if 'document_id_field' in config and config['document_id_field'] in record:
            record['osi_load_doc_id'] = record[config['document_id_field']]
        return record
