# SPDX-License-Identifier: MIT-0
"""
Delete OpenSearch index utility.
Reads workload configuration and deletes the specified index after user confirmation.
"""
import argparse
import logging
import boto3
import importlib
from opensearchpy import OpenSearch, RequestsHttpConnection

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from shared_utils import get_stack_outputs, create_opensearch_client, get_opensearch_credentials

# Hard-coded index name to match OSI pipeline configuration
DEFAULT_INDEX_NAME = "osi-load-index"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_workload_config(workload_name):
    """Load workload configuration."""
    try:
        # Add workloads to Python path
        workloads_path = Path(__file__).parent.parent / 'workloads'
        sys.path.insert(0, str(workloads_path))
        
        # Import workload config
        config_module = importlib.import_module(f"{workload_name}.config")
        return config_module.CONFIG
    except ImportError as e:
        raise Exception(f"Failed to load workload config '{workload_name}': {e}")


def delete_index(client, index_name, logger):
    """Delete OpenSearch index if it exists."""
    if client.indices.exists(index=index_name):
        client.indices.delete(index=index_name)
        logger.info(f"Successfully deleted index: {index_name}")
        return True
    else:
        logger.info(f"Index {index_name} does not exist")
        return False


def confirm_deletion(index_name):
    """Prompt user for confirmation before deleting index."""
    print(f"\n⚠️  WARNING: This will permanently delete the OpenSearch index '{index_name}' and ALL its data!")
    print("This action cannot be undone.")
    print("You will need to run 'python dataset/create_index.py <workload>' to recreate the index.")
    
    while True:
        response = input(f"\nAre you sure you want to delete index '{index_name}'? (yes/no): ").lower().strip()
        if response in ['yes', 'y']:
            return True
        elif response in ['no', 'n']:
            return False
        else:
            print("Please enter 'yes' or 'no'")


def main():
    parser = argparse.ArgumentParser(description='Delete OpenSearch index')
    parser.add_argument('workload_name', help='Name of the workload folder')
    parser.add_argument('--region', default='us-west-2', help='AWS region')
    parser.add_argument('--force', action='store_true', help='Skip confirmation prompt')
    
    args = parser.parse_args()
    
    try:
        # Load workload config to get region
        config = load_workload_config(args.workload_name)
        region = config.get("region", args.region)  # Config takes precedence, fallback to command line
        
        logger.info(f"Deleting index for workload: {args.workload_name}")
        logger.info(f"Using region: {region}")
        
        # Get stack outputs
        outputs = get_stack_outputs(region)
        
        required_outputs = ['OsiLoadOpenSearchDashboardsURL', 'OsiLoadMasterUserSecretArn']
        missing = [key for key in required_outputs if key not in outputs]
        if missing:
            raise Exception(f"Missing required stack outputs: {missing}")
        
        proxy_url = outputs['OsiLoadOpenSearchDashboardsURL']
        secret_arn = outputs['OsiLoadMasterUserSecretArn']
        
        # Get credentials from Secrets Manager
        username, password = get_opensearch_credentials(secret_arn, region)
        index_name = DEFAULT_INDEX_NAME  # Use hardcoded index name
        
        # Extract host from proxy URL (remove https://)
        proxy_host = proxy_url.replace('https://', '').replace('http://', '')
        
        logger.info(f"Using proxy endpoint: {proxy_host}, index: {index_name}")
        
        # Confirm deletion unless --force is used
        if not args.force:
            if not confirm_deletion(index_name):
                logger.info("Index deletion cancelled by user")
                return
        
        # Create OpenSearch client and delete index
        client = create_opensearch_client(proxy_host, username, password)
        
        if delete_index(client, index_name, logger):
            print(f"\n✅ Index '{index_name}' has been successfully deleted.")
            print(f"To recreate the index, run: python dataset/create_index.py {args.workload_name}")
        else:
            print(f"\nℹ️  Index '{index_name}' did not exist.")
        
    except Exception as e:
        logger.error(f"Failed to delete index: {e}")
        raise


if __name__ == '__main__':
    main()
