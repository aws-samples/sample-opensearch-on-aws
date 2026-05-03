# SPDX-License-Identifier: MIT-0
"""
Main orchestration script for loading data to S3.
"""
import argparse
import importlib
import logging
import sys
import boto3
from data_sources.batch_builder import BatchBuilder
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from shared_utils import get_stack_outputs, create_opensearch_client
from dataset.data_sources.batch_builder import BatchBuilder


# Hard-coded index name to match OSI pipeline configuration
DEFAULT_INDEX_NAME = "osi-load-index"

# Hard-coded S3 output configuration to match OSI pipeline source
DEFAULT_DEST_S3_BUCKET = "osi-load-source-bucket"
DEFAULT_DEST_S3_PREFIX = "batches/"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_data_to_s3(data_source, region: str = 'us-west-2', dest_s3_bucket: str = DEFAULT_DEST_S3_BUCKET, dest_s3_prefix: str = DEFAULT_DEST_S3_PREFIX, index_name: str = DEFAULT_INDEX_NAME):
    """Load data from data source to S3 in batches."""
    logger.info(f"Starting data loading to index: {index_name}")
    
    s3_client = boto3.client('s3', region_name=region)
    batch_count = 0
    total_records = 0
    
    for batch in data_source.process_sources([]):  # Data source discovers internally
        # Build OpenSearch bulk format
        bulk_data = BatchBuilder.build_bulk_batch(batch, index_name)
        
        # Upload to S3
        s3_key = f"{dest_s3_prefix}batch_{batch_count:06d}.json"
        s3_client.put_object(
            Bucket=dest_s3_bucket,
            Key=s3_key,
            Body=bulk_data,
            ContentType='application/json'
        )
        
        batch_count += 1
        total_records += len(batch)
        logger.info(f"Uploaded batch {batch_count} with {len(batch)} records to s3://{dest_s3_bucket}/{s3_key}")
    
    logger.info(f"Processing complete. {batch_count} batches uploaded with {total_records} total records.")


def load_workload_components(workload_name):
    """Dynamically load workload components."""
    try:
        # Add workloads to Python path
        workloads_path = Path(__file__).parent.parent / 'workloads'
        sys.path.insert(0, str(workloads_path))
        
        # Import workload components
        data_source_module = importlib.import_module(f"{workload_name}.data_source")
        config_module = importlib.import_module(f"{workload_name}.config")
        
        return data_source_module.WorkloadDataSource, config_module.CONFIG
    except ImportError as e:
        raise Exception(f"Failed to load workload '{workload_name}': {e}")


def main():
    parser = argparse.ArgumentParser(description='Load data to S3 using workload configuration')
    parser.add_argument('workload_name', help='Name of the workload to use')
    parser.add_argument('--region', default='us-west-2', help='AWS region')
    parser.add_argument('--max-documents', type=int, help='Override max_documents from config')
    parser.add_argument('--force', action='store_true', help='Skip confirmation prompt')
    
    args = parser.parse_args()
    
    # Confirmation that index has been created
    if not args.force:
        print("⚠️  Before loading data, you must create the OpenSearch index first.")
        print(f"   Run: python dataset/create_index.py {args.workload_name}")
        confirmation = input("Have you already created the index? (yes/no): ").lower().strip()
        if confirmation != 'yes' and confirmation != 'y':
            print("Please create the index first, then run this script again.")
            return
    
    logger.info(f"Starting data loading for workload: {args.workload_name}")
    
    # Load workload components
    data_source_class, config = load_workload_components(args.workload_name)
    logger.info(f"Loaded workload components successfully")
    
    # Create data source instance with region
    region = config.get("region", args.region)  # Config takes precedence, fallback to command line
    logger.info(f"Using region: {region}")
    
    # Override max_documents if provided via command line
    max_documents = args.max_documents if args.max_documents is not None else config.get("max_documents")
    
    if region and hasattr(data_source_class, '__bases__') and any('S3Source' in str(base) for base in data_source_class.__bases__):
        # S3-based workloads need region
        data_source = data_source_class(
            batch_size=config["batch_size"],
            max_documents=max_documents,
            region=region
        )
        logger.info(f"Created S3-based data source with batch_size={config['batch_size']}, max_documents={max_documents}")
    else:
        # File-based workloads don't need region
        data_source = data_source_class(
            batch_size=config["batch_size"],
            max_documents=max_documents
        )
        logger.info(f"Created file-based data source with batch_size={config['batch_size']}, max_documents={config.get('max_documents')}")
    
    # Get bucket name from CloudFormation stack outputs
    outputs = get_stack_outputs(region)
    dest_bucket = outputs.get('OsiLoadSourceBucketName')
    if not dest_bucket:
        raise Exception("Could not find OsiLoadSourceBucketName in stack outputs")
    logger.info(f"Using source bucket: {dest_bucket}")

    # Load data to S3
    load_data_to_s3(data_source, region=region, dest_s3_bucket=dest_bucket)
    logger.info("Data loading completed successfully")


if __name__ == "__main__":
    main()
