# SPDX-License-Identifier: MIT-0
"""
Empty source bucket utility.
Deletes all objects from the OSI pipeline source S3 bucket.
"""
import argparse
import logging
import boto3
from botocore.exceptions import ClientError

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from shared_utils import get_stack_outputs, create_opensearch_client


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def empty_bucket(bucket_name, region, prefix=""):
    """Delete all objects from S3 bucket with optional prefix."""
    s3_client = boto3.client('s3', region_name=region)
    
    try:
        # List all objects in the bucket
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)
        
        delete_count = 0
        
        for page in pages:
            if 'Contents' not in page:
                continue
                
            # Prepare objects for batch deletion
            objects_to_delete = []
            for obj in page['Contents']:
                objects_to_delete.append({'Key': obj['Key']})
                
            if objects_to_delete:
                # Delete objects in batches (up to 1000 per request)
                response = s3_client.delete_objects(
                    Bucket=bucket_name,
                    Delete={'Objects': objects_to_delete}
                )
                
                deleted = len(response.get('Deleted', []))
                delete_count += deleted
                logger.info(f"Deleted {deleted} objects from s3://{bucket_name}")
                
                # Log any errors
                if 'Errors' in response:
                    for error in response['Errors']:
                        logger.error(f"Failed to delete {error['Key']}: {error['Message']}")
        
        return delete_count
        
    except ClientError as e:
        logger.error(f"Error accessing bucket {bucket_name}: {e}")
        raise


def confirm_deletion(bucket_name, prefix=""):
    """Prompt user for confirmation before emptying bucket."""
    if prefix:
        print(f"\n⚠️  WARNING: This will permanently delete ALL objects with prefix '{prefix}' from S3 bucket '{bucket_name}'!")
    else:
        print(f"\n⚠️  WARNING: This will permanently delete ALL objects from S3 bucket '{bucket_name}'!")
    
    print("This action cannot be undone.")
    print("This will remove all batch files that were uploaded for OpenSearch ingestion.")
    
    while True:
        response = input(f"\nAre you sure you want to empty the bucket? (yes/no): ").lower().strip()
        if response in ['yes', 'y']:
            return True
        elif response in ['no', 'n']:
            return False
        else:
            print("Please enter 'yes' or 'no'")


def main():
    parser = argparse.ArgumentParser(description='Empty OSI pipeline source S3 bucket')
    parser.add_argument('--prefix', default='', help='Only delete objects with this prefix')
    parser.add_argument('--region', default='us-west-2', help='AWS region')
    parser.add_argument('--force', action='store_true', help='Skip confirmation prompt')
    
    args = parser.parse_args()
    
    try:
        # Get bucket name from CloudFormation stack outputs
        outputs = get_stack_outputs(args.region)
        bucket_name = outputs.get('OsiLoadSourceBucketName')
        if not bucket_name:
            raise Exception("Could not find OsiLoadSourceBucketName in stack outputs")
        
        logger.info(f"Preparing to empty bucket: {bucket_name}")
        if args.prefix:
            logger.info(f"Using prefix filter: {args.prefix}")
        logger.info(f"Using region: {args.region}")
        
        # Confirm deletion unless --force is used
        if not args.force:
            if not confirm_deletion(bucket_name, args.prefix):
                logger.info("Bucket emptying cancelled by user")
                return
        
        # Empty the bucket
        delete_count = empty_bucket(bucket_name, args.region, args.prefix)
        
        if delete_count > 0:
            print(f"\n✅ Successfully deleted {delete_count} objects from s3://{bucket_name}")
            if args.prefix:
                print(f"   (objects with prefix: {args.prefix})")
        else:
            print(f"\nℹ️  No objects found in s3://{bucket_name}")
            if args.prefix:
                print(f"   (with prefix: {args.prefix})")
        
        logger.info("Bucket emptying completed successfully")
        
    except Exception as e:
        logger.error(f"Failed to empty bucket: {e}")
        raise


if __name__ == '__main__':
    main()
