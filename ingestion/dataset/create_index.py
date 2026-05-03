# SPDX-License-Identifier: MIT-0
"""
Create the OpenSearch index from the workload settings.
Reads stack outputs and creates index using workload-specific settings.
"""
import argparse
import json
import logging
import boto3
import importlib
import sys
from opensearchpy import OpenSearch, RequestsHttpConnection
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from shared_utils import get_stack_outputs, create_opensearch_client, get_opensearch_credentials

# Hard-coded index name to match OSI pipeline configuration. If you change this,
# ensure the pipeline configuration is updated accordingly.
DEFAULT_INDEX_NAME = "osi-load-index"


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


def setup_logging():
    """Setup logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    return logging.getLogger(__name__)


def delete_index(client, index_name, logger):
    """Delete existing index if it exists."""
    if client.indices.exists(index=index_name):
        client.indices.delete(index=index_name)
        logger.info(f"Deleted existing index: {index_name}")
    else:
        logger.info(f"Index {index_name} does not exist")


def create_index(client, index_name, index_settings, logger):
    """Create OpenSearch index with settings."""
    response = client.indices.create(index=index_name, body=index_settings)
    logger.info(f"Successfully created index: {index_name}")
    return response


def main():
    logger = setup_logging()
    
    parser = argparse.ArgumentParser(description='Create OpenSearch index from workload settings')
    parser.add_argument('workload_name', help='Name of the workload folder')
    parser.add_argument('--stack-name', default='OsiLoadStack', help='CloudFormation stack name')
    parser.add_argument('--region', default='us-west-2', help='AWS region')
    parser.add_argument('--delete-existing', action='store_true', help='Delete existing index first')
    parser.add_argument('--force', action='store_true', help='Skip confirmation prompt')
    
    args = parser.parse_args()
    
    try:
        # Load workload config to get region
        config = load_workload_config(args.workload_name)
        region = config.get("region", args.region)  # Config takes precedence, fallback to command line
        
        # Load index settings from workload
        settings_path = Path(__file__).parent.parent / 'workloads' / args.workload_name / 'index_settings.json'
        if not settings_path.exists():
            raise Exception(f"Index settings not found: {settings_path}")
        
        with open(settings_path) as f:
            index_settings = json.load(f)
        
        logger.info(f"Loaded index settings for workload: {args.workload_name}")
        
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
        index_name = DEFAULT_INDEX_NAME
        
        # Extract host from proxy URL (remove https://)
        proxy_host = proxy_url.replace('https://', '').replace('http://', '')
        
        logger.info(f"Using proxy endpoint: {proxy_host}, index: {index_name}, region: {region}")
        
        # Create OpenSearch client
        client = create_opensearch_client(proxy_host, username, password)

        # Warning and confirmation
        index_exists = client.indices.exists(index=index_name)
        if not args.force and args.delete_existing:
            if index_exists:
                print(f"⚠️  WARNING: This script will delete the {index_name} index and ALL DATA in it!")
                print("   This action cannot be undone.")

                confirmation = input("Do you understand and want to continue? (yes/no): ").lower().strip()
                if confirmation != 'yes' and confirmation != 'y':
                    print("Operation cancelled.")
                    return
        elif index_exists and not args.force and not args.delete_existing:
            logging.error(f"The index {index_name} exists. use --force or --delete-existing to delete it")
            return

        # Delete existing index if requested
        if args.delete_existing:
            delete_index(client, index_name, logger)
        
        # Create index
        create_index(client, index_name, index_settings, logger)
        logger.info(f"Index creation complete for workload: {args.workload_name}")
        
    except Exception as e:
        logger.error(f"Failed to create index: {e}")
        raise


if __name__ == '__main__':
    main()
