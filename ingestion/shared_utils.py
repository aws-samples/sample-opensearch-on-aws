# SPDX-License-Identifier: MIT-0
"""
Shared utilities for the OSI Load framework.
"""
import json
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection


def get_stack_outputs(region):
    """Get CloudFormation stack outputs."""
    cf_client = boto3.client('cloudformation', region_name=region)
    try:
        response = cf_client.describe_stacks(StackName='OsiLoadStack')
        outputs = {}
        for output in response['Stacks'][0]['Outputs']:
            outputs[output['OutputKey']] = output['OutputValue']
        return outputs
    except Exception as e:
        raise Exception(f"Failed to get stack outputs: {e}")


def get_opensearch_credentials(secret_arn, region):
    """Retrieve OpenSearch credentials from Secrets Manager."""
    secrets_client = boto3.client('secretsmanager', region_name=region)
    try:
        response = secrets_client.get_secret_value(SecretId=secret_arn)
        secret = json.loads(response['SecretString'])
        return secret['username'], secret['password']
    except Exception as e:
        raise Exception(f"Failed to get credentials from Secrets Manager: {e}")


def create_opensearch_client(proxy_host, username, password):
    """Create OpenSearch client with basic auth via proxy."""
    return OpenSearch(
        hosts=[{'host': proxy_host, 'port': 443}],
        http_auth=(username, password),
        use_ssl=True,
        verify_certs=False,  # Proxy uses self-signed cert
        connection_class=RequestsHttpConnection,
        timeout=30
    )
