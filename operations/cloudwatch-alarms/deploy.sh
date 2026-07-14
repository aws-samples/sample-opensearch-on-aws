#!/bin/bash
# deploy.sh - Interactive deployment script for OpenSearch CloudWatch Alarms
#
# This script:
#   1. Lists existing OpenSearch domains (dropdown selection)
#   2. Describes the selected domain to auto-detect configuration
#   3. Computes alarm thresholds (shards, JVM, node count, storage)
#   4. Deploys the CloudFormation stack with computed values
#
# Prerequisites:
#   - AWS CLI v2 configured with appropriate credentials
#   - Permissions: es:ListDomainNames, es:DescribeDomain,
#     ec2:DescribeInstanceTypes, cloudformation:CreateStack
#
# Usage:
#   ./deploy.sh [--region us-east-1] [--profile my-profile]

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Parse arguments
REGION=""
PROFILE=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --region) REGION="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--region REGION] [--profile PROFILE]"
            echo ""
            echo "Deploys recommended CloudWatch alarms for an OpenSearch domain."
            echo "The script auto-detects your domain configuration and computes"
            echo "appropriate alarm thresholds."
            echo ""
            echo "Options:"
            echo "  --region   AWS region (default: CLI default region)"
            echo "  --profile  AWS CLI profile name"
            exit 0
            ;;
        *) echo -e "${RED}Unknown option: $1${NC}"; exit 1 ;;
    esac
done

# Build AWS CLI command
AWS_CMD="aws"
[ -n "$PROFILE" ] && AWS_CMD="$AWS_CMD --profile $PROFILE"
[ -n "$REGION" ] && AWS_CMD="$AWS_CMD --region $REGION"

# =============================================
# Prerequisites check
# =============================================

# Check AWS CLI is installed
if ! command -v aws &>/dev/null; then
    echo -e "${RED}Error: AWS CLI is not installed.${NC}"
    echo "Install it from: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    exit 1
fi

# Check python3 is available
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}Error: python3 is not installed.${NC}"
    echo "python3 is required for JSON parsing."
    exit 1
fi

# Verify credentials are configured
CALLER_IDENTITY=$($AWS_CMD sts get-caller-identity --output json 2>/dev/null) || {
    echo -e "${RED}Error: AWS credentials are not configured or are invalid.${NC}"
    echo ""
    echo "Configure credentials using one of:"
    echo "  aws configure"
    echo "  export AWS_ACCESS_KEY_ID=... && export AWS_SECRET_ACCESS_KEY=..."
    echo "  aws sso login --profile <profile>"
    echo ""
    echo "Or specify a profile: ./deploy.sh --profile <profile-name>"
    exit 1
}

ACCOUNT_ID=$(echo "$CALLER_IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
CALLER_ARN=$(echo "$CALLER_IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")

# Determine region
CURRENT_REGION=$($AWS_CMD configure get region 2>/dev/null || echo "")
[ -n "$REGION" ] && CURRENT_REGION="$REGION"
if [ -z "$CURRENT_REGION" ]; then
    echo -e "${RED}Error: No AWS region configured.${NC}"
    echo ""
    echo "Set a region using one of:"
    echo "  aws configure set region us-east-1"
    echo "  export AWS_DEFAULT_REGION=us-east-1"
    echo "  ./deploy.sh --region us-east-1"
    exit 1
fi

# =============================================
# Display context
# =============================================

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE} OpenSearch CloudWatch Alarms Deployment${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "  AWS Account:  ${GREEN}${ACCOUNT_ID}${NC}"
echo -e "  Region:       ${GREEN}${CURRENT_REGION}${NC}"
echo -e "  Identity:     ${GREEN}${CALLER_ARN}${NC}"
echo ""
echo -e "${BLUE}Required permissions:${NC}"
echo "  • es:ListDomainNames, es:DescribeDomain"
echo "  • ec2:DescribeInstanceTypes"
echo "  • cloudformation:CreateStack / UpdateStack"
echo "  • cloudwatch:PutMetricAlarm"
echo "  • sns:CreateTopic, sns:Subscribe"
echo ""

# Fetch domains
echo -e "${YELLOW}Fetching OpenSearch domains...${NC}"
DOMAINS=$($AWS_CMD opensearch list-domain-names --query 'DomainNames[].DomainName' --output text 2>/dev/null)

if [ -z "$DOMAINS" ]; then
    echo -e "${RED}No OpenSearch domains found in region ${CURRENT_REGION}.${NC}"
    exit 1
fi

IFS=$'\t' read -ra DOMAIN_ARRAY <<< "$DOMAINS"

echo ""
echo -e "${GREEN}Available domains:${NC}"
for i in "${!DOMAIN_ARRAY[@]}"; do
    echo "  $((i+1)). ${DOMAIN_ARRAY[$i]}"
done
echo ""

# Select domain
while true; do
    read -p "Select a domain (1-${#DOMAIN_ARRAY[@]}): " SELECTION
    if [[ "$SELECTION" =~ ^[0-9]+$ ]] && [ "$SELECTION" -ge 1 ] && [ "$SELECTION" -le "${#DOMAIN_ARRAY[@]}" ]; then
        DOMAIN="${DOMAIN_ARRAY[$((SELECTION-1))]}"
        break
    fi
    echo -e "${RED}Invalid selection.${NC}"
done

echo -e "Selected: ${GREEN}${DOMAIN}${NC}"
echo ""

# Get email
while true; do
    read -p "Email for notifications: " EMAIL
    if [[ "$EMAIL" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
        break
    fi
    echo -e "${RED}Invalid email format.${NC}"
done

echo ""
echo -e "${YELLOW}Detecting domain configuration...${NC}"

# Describe domain - get cluster config and EBS options
DOMAIN_STATUS=$($AWS_CMD opensearch describe-domain --domain-name "$DOMAIN" --query 'DomainStatus' --output json)

DATA_INSTANCE_TYPE=$(echo "$DOMAIN_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ClusterConfig',{}).get('InstanceType','r6g.large.search'))")
DATA_INSTANCE_COUNT=$(echo "$DOMAIN_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ClusterConfig',{}).get('InstanceCount',3))")
MASTER_ENABLED=$(echo "$DOMAIN_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ClusterConfig',{}).get('DedicatedMasterEnabled',False))")
MASTER_TYPE=$(echo "$DOMAIN_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ClusterConfig',{}).get('DedicatedMasterType',''))")
EBS_VOLUME_SIZE=$(echo "$DOMAIN_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('EBSOptions',{}).get('VolumeSize',100))")

# Get instance memory via EC2 API
EC2_TYPE=$(echo "$DATA_INSTANCE_TYPE" | sed 's/.search$//')
RAM_MIB=$($AWS_CMD ec2 describe-instance-types --instance-types "$EC2_TYPE" --query 'InstanceTypes[0].MemoryInfo.SizeInMiB' --output text 2>/dev/null || echo "65536")
RAM_GIB=$((RAM_MIB / 1024))

# Compute JVM heap (RAM/2, capped at 32)
HEAP_GIB=$((RAM_GIB / 2))
[ "$HEAP_GIB" -gt 32 ] && HEAP_GIB=32

# Compute shards threshold: 25 x heap x nodes
SHARDS_THRESHOLD=$((25 * HEAP_GIB * DATA_INSTANCE_COUNT))

# Compute free storage space thresholds (in MiB)
# WARNING: min(25% of disk, 25 GiB) — whichever is less
STORAGE_25_PCT=$((EBS_VOLUME_SIZE * 1024 * 25 / 100))
WARNING_CAP=25600  # 25 GiB in MiB
if [ "$STORAGE_25_PCT" -lt "$WARNING_CAP" ]; then
    STORAGE_WARNING_MIB=$STORAGE_25_PCT
else
    STORAGE_WARNING_MIB=$WARNING_CAP
fi

# CRITICAL: min(20% of disk, 20 GiB) — whichever is less
STORAGE_20_PCT=$((EBS_VOLUME_SIZE * 1024 * 20 / 100))
CRITICAL_CAP=20480  # 20 GiB in MiB
if [ "$STORAGE_20_PCT" -lt "$CRITICAL_CAP" ]; then
    STORAGE_CRITICAL_MIB=$STORAGE_20_PCT
else
    STORAGE_CRITICAL_MIB=$CRITICAL_CAP
fi

# Determine if dedicated master nodes are present
HAS_MASTER_NODES="false"
if [ "$MASTER_ENABLED" = "True" ]; then
    HAS_MASTER_NODES="true"
fi

# Detect old-gen instances
OLD_GEN_DATA="false"
OLD_GEN_MASTER="false"
if echo "$DATA_INSTANCE_TYPE" | grep -qE '^(m3|m4|r3|r4|c4|i2|t2)\.'; then
    OLD_GEN_DATA="true"
fi
if echo "$MASTER_TYPE" | grep -qE '^(m3|m4|r3|r4|c4|i2|t2)\.'; then
    OLD_GEN_MASTER="true"
fi

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE} Detected Configuration${NC}"
echo -e "${BLUE}========================================${NC}"
echo -e "  Data instance type:      ${GREEN}${DATA_INSTANCE_TYPE}${NC}"
echo -e "  Data node count:         ${GREEN}${DATA_INSTANCE_COUNT}${NC}"
echo -e "  Instance RAM:            ${GREEN}${RAM_GIB} GiB${NC}"
echo -e "  JVM heap per node:       ${GREEN}${HEAP_GIB} GiB${NC}"
echo -e "  EBS volume per node:     ${GREEN}${EBS_VOLUME_SIZE} GiB${NC}"
echo -e "  Shards threshold:        ${GREEN}${SHARDS_THRESHOLD}${NC} (25 x ${HEAP_GIB} x ${DATA_INSTANCE_COUNT})"
echo -e "  Free storage warning:    ${GREEN}${STORAGE_WARNING_MIB} MiB${NC} (min(25% of ${EBS_VOLUME_SIZE} GiB, 25 GiB))"
echo -e "  Free storage critical:   ${GREEN}${STORAGE_CRITICAL_MIB} MiB${NC} (min(20% of ${EBS_VOLUME_SIZE} GiB, 20 GiB))"
echo -e "  Dedicated master nodes:  ${GREEN}${HAS_MASTER_NODES}${NC}"
echo -e "  Old-gen data nodes:      ${GREEN}${OLD_GEN_DATA}${NC}"
if [ "$MASTER_ENABLED" = "True" ]; then
    echo -e "  Master node type:        ${GREEN}${MASTER_TYPE}${NC}"
    echo -e "  Old-gen master nodes:    ${GREEN}${OLD_GEN_MASTER}${NC}"
fi
echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE} Deployment Summary${NC}"
echo -e "${BLUE}========================================${NC}"
echo -e "  Stack name: ${GREEN}opensearch-alarms-${DOMAIN}${NC}"
echo -e "  Email:      ${GREEN}${EMAIL}${NC}"
echo -e "  Alarms:     ${GREEN}23 CloudWatch alarms${NC}"
echo ""

read -p "Deploy? (y/n): " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "Cancelled."
    exit 0
fi

echo ""
echo -e "${YELLOW}Deploying CloudFormation stack...${NC}"

TEMPLATE_FILE="$(dirname "$0")/OpenSearch_cloudwatch_alarms.yaml"

# Check if stack already exists (update vs create)
STACK_NAME="opensearch-alarms-${DOMAIN}"
STACK_EXISTS=$($AWS_CMD cloudformation describe-stacks --stack-name "$STACK_NAME" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")

CFN_ACTION="create-stack"
if [ "$STACK_EXISTS" != "DOES_NOT_EXIST" ]; then
    echo -e "${YELLOW}Stack already exists (status: ${STACK_EXISTS}). Updating...${NC}"
    CFN_ACTION="update-stack"
fi

$AWS_CMD cloudformation "$CFN_ACTION" \
    --stack-name "$STACK_NAME" \
    --template-body "file://${TEMPLATE_FILE}" \
    --parameters \
        ParameterKey=OpenSearchDomainName,ParameterValue="${DOMAIN}" \
        ParameterKey=Email,ParameterValue="${EMAIL}" \
        ParameterKey=NumberOfDataNodes,ParameterValue="${DATA_INSTANCE_COUNT}" \
        ParameterKey=ShardsThreshold,ParameterValue="${SHARDS_THRESHOLD}" \
        ParameterKey=FreeStorageSpaceWarningThreshold,ParameterValue="${STORAGE_WARNING_MIB}" \
        ParameterKey=FreeStorageSpaceCriticalThreshold,ParameterValue="${STORAGE_CRITICAL_MIB}" \
        ParameterKey=HasDedicatedMasterNodes,ParameterValue="${HAS_MASTER_NODES}" \
        ParameterKey=IsOldGenDataNodes,ParameterValue="${OLD_GEN_DATA}" \
        ParameterKey=IsOldGenMasterNodes,ParameterValue="${OLD_GEN_MASTER}" \
    --tags \
        Key=Purpose,Value=OpenSearchMonitoring \
        Key=ManagedBy,Value=CloudFormation \
        Key=Domain,Value="${DOMAIN}"

echo ""
echo -e "${GREEN}✓ Stack ${CFN_ACTION} initiated!${NC}"
echo ""
echo "Next steps:"
if [ "$CFN_ACTION" = "create-stack" ]; then
    echo "  1. Confirm the SNS subscription in your email (${EMAIL})"
    echo "  2. Monitor stack progress:"
else
    echo "  1. Monitor stack progress:"
fi
echo "     $AWS_CMD cloudformation describe-stacks --stack-name ${STACK_NAME} --query 'Stacks[0].StackStatus'"
echo ""
echo -e "${YELLOW}Note: Re-run this script if you resize your domain to update thresholds.${NC}"
