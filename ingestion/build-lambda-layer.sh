#!/bin/bash
# Copyright OpenSearch Ingestion Load contributors
# SPDX-License-Identifier: Apache-2.0

# Build Lambda Layer for OpenSearch dependencies

echo "Building Lambda layer..."

# Create lambda-layer directory structure
mkdir -p lambda-layer/python

# Install dependencies to the lambda layer
pip install opensearch-py -t lambda-layer/python/

echo "Lambda layer built successfully in lambda-layer/"
echo "The CDK stack will automatically use this layer when deployed."
