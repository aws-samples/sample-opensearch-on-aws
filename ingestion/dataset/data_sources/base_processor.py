# SPDX-License-Identifier: MIT-0
"""
Base processor with template method pattern for data loading pipeline.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Iterator


class BaseProcessor(ABC):
    """Abstract base processor defining the data loading pipeline."""
    
    def __init__(self, batch_size: int = 100):
        self.batch_size = batch_size
    
    @abstractmethod
    def transform_data(self, raw_data: Any) -> Any:
        """User implements: data cleansing and enrichment."""
        pass
    
    @abstractmethod
    def process_records(self, data: Any) -> Iterator[Dict[str, Any]]:
        """User implements: convert data to JSON records."""
        pass
