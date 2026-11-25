#!/bin/bash
# PUT the two index templates to the domain. Needs awscurl (pip install awscurl) and credentials of an FGAC admin.
# Usage: scripts/install-templates.sh rendered/templates <domain-endpoint> <region>
set -euo pipefail
DIR=${1:?templates dir}; HOST=${2:?domain endpoint}; REGION=${3:?region}
awscurl --service es --region "$REGION" -X PUT "https://$HOST/_index_template/otel-spans" -H "Content-Type: application/json" -d @"$DIR/otel-spans.json"; echo
awscurl --service es --region "$REGION" -X PUT "https://$HOST/_index_template/otel-logs"  -H "Content-Type: application/json" -d @"$DIR/otel-logs.json"; echo
