#!/bin/bash
# Three numbers that must agree: index doc counts, the pipeline's documentsSuccess metric (CloudWatch lags the index by a few minutes), and an empty dead-letter bucket.
# _count and _search are not available on an Optimized domain; _cat/indices and PPL `stats count()` are.
set -euo pipefail
HOST=${1:?domain endpoint}; REGION=${2:?region}; PIPELINE=${3:?pipeline name}; DLQ=${4:?dlq bucket}
# Python for the time window: the relative-time option of GNU date does not exist on macOS. python3 on Linux and macOS; python on Windows,
# where python3 can be the Microsoft Store stub.
PY=python3; "$PY" -c "" >/dev/null 2>&1 || PY=python
START=$("$PY" -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) - d.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
END=$("$PY" -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')
awscurl --service es --region "$REGION" "https://$HOST/_cat/indices/otel-v2-apm-span-*,logs-otel-v1-*,otel-v2-apm-service-map*?v&h=index,health,docs.count,pri.store.size"
aws cloudwatch get-metric-statistics --region "$REGION" --namespace AWS/OSIS --metric-name "otel-traces-pipeline.opensearch.documentsSuccess.count" \
  --dimensions Name=PipelineName,Value="$PIPELINE" --start-time "$START" --end-time "$END" --period 3600 --statistics Sum --output table
echo "dead-letter objects (should be none):"; aws s3 ls "s3://$DLQ/" --recursive | tail -5
