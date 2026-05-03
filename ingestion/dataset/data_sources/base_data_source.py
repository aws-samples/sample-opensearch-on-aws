# SPDX-License-Identifier: MIT-0
"""
Base data source with template method pattern for data loading pipeline.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Iterator


class BaseDataSource(ABC):
    """Abstract base data source defining the data loading pipeline."""
    
    def __init__(self, batch_size: int = 100, max_documents: int = None, region: str = None):
        self.batch_size = batch_size
        self.max_documents = max_documents
        self.region = region
    
    @abstractmethod
    def process_sources(self, sources: List[str]) -> Iterator[List[Dict[str, Any]]]:
        """Abstract method - subclasses implement source-specific processing."""
        pass
    
    @abstractmethod
    def transform_data(self, raw_data: Any) -> Any:
        """User implements: data cleansing and enrichment."""
        pass
    
    @abstractmethod
    def process_records(self, data: Any) -> Iterator[Dict[str, Any]]:
        """User implements: convert data to JSON records."""
        pass
