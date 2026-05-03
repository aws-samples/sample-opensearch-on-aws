# SPDX-License-Identifier: MIT-0
"""
Reset OSI pipeline utility.
Stops the OpenSearch Ingestion pipeline, waits for stopped state, then starts it again.
"""
import argparse
import logging
import boto3
import time
from botocore.exceptions import ClientError

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from shared_utils import get_stack_outputs


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_pipeline_status(osis_client, pipeline_name):
    """Get current pipeline status."""
    try:
        response = osis_client.get_pipeline(PipelineName=pipeline_name)
        return response['Pipeline']['Status']
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            raise Exception(f"Pipeline '{pipeline_name}' not found")
        else:
            raise Exception(f"Error getting pipeline status: {e}")


def stop_pipeline(osis_client, pipeline_name):
    """Stop the OSI pipeline."""
    try:
        logger.info(f"Stopping pipeline: {pipeline_name}")
        osis_client.stop_pipeline(PipelineName=pipeline_name)
        logger.info("Stop command sent successfully")
    except ClientError as e:
        raise Exception(f"Error stopping pipeline: {e}")


def start_pipeline(osis_client, pipeline_name):
    """Start the OSI pipeline."""
    try:
        logger.info(f"Starting pipeline: {pipeline_name}")
        osis_client.start_pipeline(PipelineName=pipeline_name)
        logger.info("Start command sent successfully")
    except ClientError as e:
        raise Exception(f"Error starting pipeline: {e}")


def wait_for_status(osis_client, pipeline_name, target_status, timeout_minutes=10):
    """Wait for pipeline to reach target status."""
    logger.info(f"Waiting for pipeline to reach '{target_status}' status...")
    
    timeout_seconds = timeout_minutes * 60
    start_time = time.time()
    check_interval = 10  # Check every 10 seconds
    
    while True:
        current_status = get_pipeline_status(osis_client, pipeline_name)
        elapsed = int(time.time() - start_time)
        
        logger.info(f"Current status: {current_status} (elapsed: {elapsed}s)")
        
        if current_status == target_status:
            logger.info(f"✅ Pipeline reached '{target_status}' status")
            return True
        
        if elapsed >= timeout_seconds:
            logger.error(f"❌ Timeout waiting for '{target_status}' status after {timeout_minutes} minutes")
            return False
        
        if current_status in ['CREATE_FAILED', 'UPDATE_FAILED', 'START_FAILED']:
            logger.error(f"❌ Pipeline is in failed state: {current_status}")
            return False
        
        # Intentional sleep to poll pipeline status at regular intervals
        time.sleep(check_interval)  # nosec B311 - intentional polling delay


def reset_pipeline(pipeline_name, region, timeout_minutes=10):
    """Reset pipeline by stopping and starting it."""
    osis_client = boto3.client('osis', region_name=region)
    
    # Get initial status
    initial_status = get_pipeline_status(osis_client, pipeline_name)
    logger.info(f"Initial pipeline status: {initial_status}")
    
    # Stop pipeline if not already stopped
    if initial_status != 'STOPPED':
        stop_pipeline(osis_client, pipeline_name)
        
        # Wait for stopped status
        if not wait_for_status(osis_client, pipeline_name, 'STOPPED', timeout_minutes):
            raise Exception("Failed to stop pipeline within timeout")
    else:
        logger.info("Pipeline is already stopped")
    
    # Start pipeline
    start_pipeline(osis_client, pipeline_name)
    
    # Wait for active status
    if not wait_for_status(osis_client, pipeline_name, 'ACTIVE', timeout_minutes):
        raise Exception("Failed to start pipeline within timeout")
    
    logger.info("🎉 Pipeline reset completed successfully!")


def main():
    parser = argparse.ArgumentParser(description='Reset OSI pipeline by stopping and starting it')
    parser.add_argument('--region', default='us-west-2', help='AWS region')
    parser.add_argument('--timeout', type=int, default=10, help='Timeout in minutes for each state change')
    
    args = parser.parse_args()
    
    try:
        # Get pipeline name from stack outputs
        outputs = get_stack_outputs(args.region)
        pipeline_name = "osi-load-pipeline"  # Hard-coded since it's consistent in the stack
        
        logger.info(f"Starting pipeline reset for: {pipeline_name}")
        logger.info(f"Using region: {args.region}")
        logger.info(f"Timeout per operation: {args.timeout} minutes")
        
        reset_pipeline(pipeline_name, args.region, args.timeout)
        
        print(f"\n✅ Pipeline '{pipeline_name}' has been successfully reset!")
        print("The pipeline is now active and ready to process data.")
        
    except Exception as e:
        logger.error(f"Failed to reset pipeline: {e}")
        print(f"\n❌ Pipeline reset failed: {e}")
        raise


if __name__ == '__main__':
    main()
