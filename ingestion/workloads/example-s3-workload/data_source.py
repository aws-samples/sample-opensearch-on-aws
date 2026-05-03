# SPDX-License-Identifier: MIT-0
"""
Example S3 workload data source.
"""
from dataset.data_sources.s3_source import S3Source
from .transformer import WorkloadTransformer


class WorkloadDataSource(S3Source, WorkloadTransformer):
    """Example S3-based data source."""
    
    def __init__(self, batch_size: int = 100, max_documents: int = None, region: str = 'us-west-2'):
        # Import here to avoid circular imports
        from .config import CONFIG
        super().__init__(CONFIG["s3_bucket"], batch_size, max_documents, region)
    
    def process_sources(self, sources):
        """Override to discover S3 objects from config instead of using provided sources."""
        # Import here to avoid circular imports
        from .config import CONFIG
        
        # Use the generator directly in process_sources
        return super().process_sources([])  # Pass empty list since we override the discovery
