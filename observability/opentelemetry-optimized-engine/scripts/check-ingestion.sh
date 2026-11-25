#!/bin/bash
# Three numbers that must agree: index doc counts, the pipeline's documentsSuccess metric (CloudWatch lags the index by a few minutes), and an empty dead-letter bucket.
# _count and _search are not available on an Optimized domain; _cat/indices and PPL `stats count()` are.
set -euo pipefail
HOST=${1:?domain endpoint}; REGION=${2:?region}; PIPELINE=${3:?pipeline name}; DLQ=${4:?dlq bucket}
awscurl --service es --region "$REGION" "https://$HOST/_cat/indices/otel-v2-apm-span-*,logs-otel-v1-*,otel-v2-apm-service-map*?v&h=index,health,docs.count,pri.store.size"
aws cloudwatch get-metric-statistics --region "$REGION" --namespace AWS/OSIS --metric-name "otel-traces-pipeline.opensearch.documentsSuccess.count" \
  --dimensions Name=PipelineName,Value="$PIPELINE" --start-time "$(date -u -d '-1 hour' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" --period 3600 --statistics Sum --output table
echo "dead-letter objects (should be none):"; aws s3 ls "s3://$DLQ/" --recursive | tail -5
