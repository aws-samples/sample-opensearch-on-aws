# SPDX-License-Identifier: MIT-0
"""
Example file workload data source.
"""
import logging
from dataset.data_sources.file_source import FileSource
from .transformer import WorkloadTransformer

logger = logging.getLogger(__name__)


class WorkloadDataSource(WorkloadTransformer, FileSource):
    """Example file-based data source."""
    
    def __init__(self, batch_size: int = 100, max_documents: int = None, region: str = None):
        super().__init__(batch_size, max_documents, region)
    
    def process_sources(self, sources):
        """Override to discover files from config instead of using provided sources."""
        # Import here to avoid circular imports
        from .config import CONFIG
        
        # Discover files using FileSource.find_files
        files = self.find_files(CONFIG["file_patterns"], CONFIG.get("base_paths"))
        logger.info(f"Found {len(files)} files to process")
        
        # Process discovered files
        return super().process_sources(files)
