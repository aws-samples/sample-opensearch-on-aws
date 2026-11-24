# OpenSearch Ingestion pipeline

`pipeline.yaml` is the OpenSearch Ingestion (OSIS) configuration that receives OTLP from the OpenTelemetry
Collector and writes spans and logs into an OpenSearch Serverless **time series** collection.

## Why a custom index type

Serverless sinks use `index_type: custom`, so the index name and mapping that the trace-analytics presets supply
on a domain are provided here explicitly:

| Preset behaviour | Here |
|---|---|
| Index name `otel-v1-apm-span-*` | `index: "otel-v1-apm-span-r4-%{yyyy.MM.dd}"` (keep the `otel-v1-apm-span-` prefix: the Agent Traces UI pattern depends on it) |
| Mapping | `index-templates/otel-v1-apm-span.json`, applied with `PUT _index_template/...` before the first write |

## What the processors do

1. **`otel_traces`**: converts OTLP spans into the Trace Analytics document shape (`traceId`, `spanId`, `parentSpanId`,
   `startTime`, `durationInNanos`, `serviceName`, `attributes.*`).
2. **`rename_keys`**: attribute keys that different services send as a scalar in one place and an object in
   another (`rpc.system`, `db.system`, `code.function`, `user_agent`, `upstream_cluster`, `demo.ad.context_keys`,
   `error`). A field cannot be both; the scalar form is renamed to a sibling key so neither span is rejected. The
   Collector `transform` below handles `error` earlier for every sink; the pipeline rename is the safety net.
3. **`copy_values startTime -> @timestamp`**: time series collections reject documents without the index's timestamp
   field. Alternative: set `index.timestamp_field: startTime` in the template and drop this step.
4. **`copy_values attributes/gen_ai.* -> gen_ai.*`**: the Agent Traces page reads `gen_ai.operation.name`,
   `gen_ai.usage.*`, `gen_ai.request.model`, etc. at the document root as keyword/long. OSIS stores span attributes
   under `attributes.*`, so the ones the UI keys on are lifted.
5. **Root-span rule (`add_entries`)**: Agent Traces lists only spans with an empty `parentSpanId` and a recognised
   `gen_ai.operation.name`. The OTel Demo agent (Traceloop instrumentation) parents its agent span under the HTTP
   server span and names the operation `invoke_workflow`. The rule blanks `parentSpanId` on the outermost agent
   span (the only one carrying token totals) and relabels it `invoke_agent`, so each conversation appears as one
   row with tokens, and the rest of the tree hangs beneath it. Remove this block if your agent already emits a
   semconv `invoke_agent` root span.
6. **Logs**: `time -> @timestamp` and `resource/attributes/service.name -> serviceName` so Discover can filter by service.

## Deploy

`../deploy/02_pipeline.py` applies the templates with `opensearch-py` (SigV4), creates the DLQ bucket and the role,
fills the placeholders in `pipeline.yaml`, validates it with `ValidatePipeline`, and creates the pipeline with
CloudWatch logging to `/aws/vendedlogs/OpenSearchIngestion/<name>-otlp/audit-logs`. The rendered configuration is
saved next to the script.

Changing a live pipeline: delete and recreate rather than update in place. The endpoint keeps its DNS name; allow a
few minutes after the pipeline reports ACTIVE before it accepts connections. The Collector queues meanwhile.
Restart any OTel SDK whose exporter reports "Queue full" afterwards.
## Collector side

In the OpenTelemetry Demo this goes into `src/otel-collector/otelcol-config-extras.yml` (loaded last; array keys
replace upstream values, so upstream exporters must be repeated in the `traces` and `logs` pipelines, see
`../deploy/03_host.py` for the complete file). Credentials come from the EC2 instance role via IMDS (hop limit 2).

```yaml
extensions:
  sigv4auth: { region: <region>, service: osis }
processors:
  batch: {}
  # Some instrumentations set a bare string attribute `error`; others use the `error.*` namespace (error.type,
  # error.message). A field cannot be both an object and a scalar, so spans with the bare form would be rejected by
  # the sink. Move the scalar into the namespace before export.
  transform/error-attr:
    error_mode: ignore
    trace_statements:
      - context: span
        statements:
          - set(attributes["error.message"], attributes["error"]) where attributes["error"] != nil and attributes["error.message"] == nil
          - delete_key(attributes, "error") where attributes["error"] != nil
exporters:
  otlphttp/osis-traces:
    traces_endpoint: https://<pipeline-endpoint>/<prefix>/v1/traces
    auth: { authenticator: sigv4auth }
    compression: none
    sending_queue: { enabled: true, num_consumers: 4, queue_size: 2000 }
    retry_on_failure: { enabled: true, max_elapsed_time: 120s }
  otlphttp/osis-logs:
    logs_endpoint: https://<pipeline-endpoint>/<prefix>/v1/logs
    auth: { authenticator: sigv4auth }
    compression: none
    sending_queue: { enabled: true, num_consumers: 2, queue_size: 2000 }
    retry_on_failure: { enabled: true, max_elapsed_time: 120s }
service:
  extensions: [opamp, sigv4auth]          # arrays replace: repeat the demo's opamp extension
  pipelines:
    traces/osis:
      receivers: [otlp]
      # the demo's own chain (defined in its base config) plus ours; gen_ai_normalizer is REQUIRED: it turns Traceloop's
      # traceloop.span.kind into gen_ai.operation.name, which pipeline.yaml's root-span rule keys on
      processors: [resource_detection, memory_limiter, transform/sanitize_spans, gen_ai_normalizer, transform/redact_sensitive_data, redaction, transform/error-attr, batch]
      exporters: [otlphttp/osis-traces]
    logs/osis:
      receivers: [otlp]
      processors: [resource_detection, memory_limiter, transform/sanitize_logs, batch]
      exporters: [otlphttp/osis-logs]
```

If you are not using the OpenTelemetry Demo, replace the demo processors with your own; the only requirement for the
Agent Traces root rule is that `gen_ai.operation.name` is set on the agent's outermost span (semconv `invoke_agent`,
or `invoke_workflow` from the openllmetry normaliser).

Give each OSIS exporter its own Collector pipeline with a bounded `sending_queue`, so a slow or unavailable
destination drops only its own data and never causes the receiver to reject batches for the others.
