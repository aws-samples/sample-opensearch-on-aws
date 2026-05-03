# SPDX-License-Identifier: MIT-0
"""
S3 source for discovering and accessing S3 objects.
"""
import boto3
import logging
from typing import List, Dict, Any, Iterator
from botocore.exceptions import ClientError
from .base_data_source import BaseDataSource

logger = logging.getLogger(__name__)


class S3Source(BaseDataSource):
    """S3-based data source with document limit support."""
    
    def __init__(self, bucket_name: str, batch_size: int = 100, max_documents: int = None, region: str = 'us-west-2'):
        super().__init__(batch_size, max_documents, region)
        self.bucket_name = bucket_name
        self.s3_client = boto3.client('s3', region_name=region)
    
    def find_objects(self, prefixes: List[str] = None) -> Iterator[str]:
        """Generator that yields S3 objects one at a time."""
        if prefixes is None:
            prefixes = ['']
        
        for prefix in prefixes:
            try:
                paginator = self.s3_client.get_paginator('list_objects_v2')
                pages = paginator.paginate(Bucket=self.bucket_name, Prefix=prefix)
                
                for page in pages:
                    if 'Contents' in page:
                        for obj in page['Contents']:
                            yield f"s3://{self.bucket_name}/{obj['Key']}"
            except ClientError as e:
                logger.error(f"Error listing objects with prefix '{prefix}': {e}")
    
    def process_sources(self, sources: List[str]) -> Iterator[List[Dict[str, Any]]]:
        """Process S3 sources with document limit support."""
        total_processed = 0
        
        # Use provided sources or discover from subclass
        if not sources:
            # Subclass should override this method to provide discovery logic
            return
        
        batch = []
        for s3_uri in sources:
            if self.max_documents and total_processed >= self.max_documents:
                break
                
            logger.info(f"Processing {s3_uri}")
            raw_data = self.read_s3_object(s3_uri)
            cleaned_data = self.transform_data(raw_data)
            
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
    
    def read_s3_object(self, s3_uri: str) -> str:
        """Read S3 object content as string."""
        # Extract bucket and key from s3://bucket/key
        parts = s3_uri.replace('s3://', '').split('/', 1)
        bucket, key = parts[0], parts[1]
        
        try:
            response = self.s3_client.get_object(Bucket=bucket, Key=key)
            return response['Body'].read().decode('utf-8')
        except ClientError as e:
            raise Exception(f"Error reading S3 object {s3_uri}: {e}")
