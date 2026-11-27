#!/bin/bash
# PUT the two index templates to the domain. Needs awscurl (pip install awscurl), jq, and credentials of an FGAC admin.
# Usage: scripts/install-templates.sh rendered/templates <domain-endpoint> <region>
#        scripts/install-templates.sh rendered/templates/general-purpose <gp-domain-endpoint> <region>   (storage comparison)
# Exits non-zero unless the domain acknowledges each template: awscurl exits 0 on HTTP errors, so the response body is checked.
set -euo pipefail
DIR=${1:?templates dir}; HOST=${2:?domain endpoint}; REGION=${3:?region}
put_template() {
  local name=$1 file=$2 out
  out=$(awscurl --service es --region "$REGION" -X PUT "https://$HOST/_index_template/$name" -H "Content-Type: application/json" -d @"$file")
  if echo "$out" | jq -e '.acknowledged == true' >/dev/null 2>&1; then
    echo "template $name: installed"
  else
    echo "template $name: NOT installed. Response: $out" >&2
    exit 1
  fi
}
put_template otel-spans "$DIR/otel-spans.json"
put_template otel-logs  "$DIR/otel-logs.json"
