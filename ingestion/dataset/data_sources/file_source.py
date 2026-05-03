# SPDX-License-Identifier: MIT-0
"""
Generic file source for discovering local files.
"""
import glob
from typing import List, Dict, Any, Iterator
from pathlib import Path
from .base_data_source import BaseDataSource


class FileSource(BaseDataSource):
    """File-based data source with document limit support."""
    
    def __init__(self, batch_size: int = 100, max_documents: int = None, region: str = None):
        super().__init__(batch_size, max_documents, region)
    
    @staticmethod
    def find_files(patterns: List[str], base_paths: List[str] = None) -> List[str]:
        """Find files matching patterns in specified paths."""
        if base_paths is None:
            base_paths = ['.']
        
        files = []
        for base_path in base_paths:
            for pattern in patterns:
                full_pattern = str(Path(base_path) / pattern)
                files.extend(glob.glob(full_pattern, recursive=True))
        
        return sorted(list(set(files)))  # Remove duplicates and sort
    
    def read_file(self, file_path: str) -> Any:
        """Generic file reader - override for specific formats."""
        with open(file_path, 'r') as f:
            return f.read()
    
    def process_sources(self, sources: List[str]) -> Iterator[List[Dict[str, Any]]]:
        """Process file sources with document limit support."""
        total_processed = 0
        
        for file_path in sources:
            if self.max_documents and total_processed >= self.max_documents:
                break
                
            raw_data = self.read_file(file_path)
            cleaned_data = self.transform_data(raw_data)
            
            batch = []
            for record in self.process_records(cleaned_data):
                if self.max_documents and total_processed >= self.max_documents:
                    break
                    
                batch.append(record)
                total_processed += 1
                
                if len(batch) >= self.batch_size:
                    yield batch
                    batch = []
            
            if batch:  # Yield remaining records
                yield batch
