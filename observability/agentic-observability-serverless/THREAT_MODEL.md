# Threat Model: Agentic observability on Amazon OpenSearch Serverless

## 1. System overview

This sample runs the OpenTelemetry Demo (an e-commerce application with an AI shopping assistant) on an EC2 instance,
sends its telemetry through Amazon OpenSearch Ingestion into an Amazon OpenSearch Serverless time series collection,
and scores the assistant's conversations with an evaluation model on Amazon Bedrock. Results are explored in
OpenSearch UI.

Components:

- EC2 instance (Amazon Linux 2023, SSM-managed, no inbound ports) running Docker Compose: the OpenTelemetry Demo
  services, the LangGraph shopping assistant, a LiteLLM proxy, and the OpenTelemetry Collector
- Amazon Bedrock (Claude models) called by the assistant through LiteLLM and by the evaluation job
- Amazon OpenSearch Ingestion pipeline (OTLP source, SigV4-authenticated)
- Amazon OpenSearch Serverless time series collection (spans, logs, evaluation results)
- OpenSearch UI application and workspace
- Evaluation job (Python) running on an operator workstation or the EC2 host
- Optional: Amazon Managed Service for Prometheus for metrics

## 2. Data flow

```
[Shoppers / load generator] → [Demo services + AI assistant] → [LiteLLM] → [Amazon Bedrock]
                                        │ OTLP
                                        ▼
                               [OTel Collector] ── OTLP/HTTP + SigV4 ──► [OpenSearch Ingestion] ──► [OpenSearch Serverless]
                                                                                                          ▲        │
[Evaluation job] ── reads spans / writes scores (SigV4) ──────────────────────────────────────────────────┘        │
        │                                                                                                          ▼
        └── question + answer + tool results ──► [Amazon Bedrock evaluation model]                        [OpenSearch UI]
[Operator] ── SSM Session Manager ──► [EC2 host]
```

## 3. Trust boundaries

| Boundary | Components | Trust level |
|---|---|---|
| Internet | None exposed. The shop UI is reached only via SSM port forwarding | Untrusted |
| EC2 host | Demo containers, LiteLLM (loopback only), Collector | Semi-trusted (synthetic traffic, instance role) |
| AWS services | Bedrock, OpenSearch Ingestion, OpenSearch Serverless, OpenSearch UI, IAM, CloudWatch | Trusted |
| Operator workstation | Evaluation job, AWS CLI, browser | Trusted (IAM identity) |

## 4. Threat analysis (STRIDE)

### 4.1 Spoofing

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| S1 | Unauthenticated telemetry sent to the ingestion endpoint | OpenSearch Ingestion | MEDIUM | Endpoint requires SigV4 (`osis:Ingest`); unauthenticated requests are rejected and logged. Scope `osis:Ingest` to the pipeline ARN and the Collector's role only |
| S2 | Unauthorized access to the collection | OpenSearch Serverless | HIGH | IAM-only authentication; data access policy lists the pipeline role, the evaluation identity and the UI principals explicitly. Network policy restricts the endpoint (public by default in this sample; use a VPC endpoint for production) |
| S3 | Impersonating the LiteLLM proxy or calling it from outside | LiteLLM | LOW | Bound to `127.0.0.1` and the Docker network; requires a master key held in a root-only file; Bedrock credentials come from the instance role, never from the proxy's clients |
| S4 | Access to the EC2 host | EC2 | MEDIUM | No inbound security-group rules; SSM Session Manager only (IAM-authenticated, logged) |

### 4.2 Tampering

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| T1 | Modified pipeline configuration | OpenSearch Ingestion | LOW | IAM controls `osis:UpdatePipeline`; configuration kept in version control |
| T2 | Malicious or malformed telemetry altering stored data | Collector / pipeline | LOW | Only the host's Collector can sign requests; index templates fix field types; rejected documents are logged by the pipeline |
| T3 | Evaluation scores forged or altered | agent-evals index | MEDIUM | The sample's data access policy grants `aoss:*` to every listed principal; for production, give the evaluation job its own role with write access to `agent-evals` only and viewers read-only access. Each document records evaluator model, run id and version |
| T4 | Prompt injection via shopper input reaching the evaluation model | Evaluation job | MEDIUM | Evidence is passed as data with a fixed system rubric; the evaluation model has no tools. Scores are parsed and clamped (1–5, boolean) before storage; the free-text `explanation` field is truncated to 500 characters |

### 4.3 Repudiation

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| R1 | Untracked configuration or data changes | All AWS services | LOW | CloudTrail records control-plane calls; pipeline logs to CloudWatch; SSM session logging recommended for the host |
| R2 | Evaluation decisions without provenance | agent-evals | LOW | Every score stores `judge_model`, `eval_run_id`, `eval_version`, `scored_at`, the question, answer, tool names and the trace id; the full tool results are not stored and can be re-read from the trace |

### 4.4 Information disclosure

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| I1 | Personal data in traces (conversation text, customer details in tool arguments) | Spans, evaluation index, DLQ bucket | HIGH for production use | The demo uses synthetic shoppers only. The Collector pipeline that feeds OpenSearch Ingestion runs the demo's redaction processors (e-mail hashed, card number masked). For real traffic: do not record `gen_ai.input.messages` / `gen_ai.output.messages` / tool arguments without a data-protection review; restrict read access with data access policies; set `index.data.retention`; add a lifecycle rule to the DLQ bucket |
| I2 | Conversation content sent to the evaluation model | Amazon Bedrock | MEDIUM | Bedrock does not use customer content to train models; data stays in-region. Still subject to I1 review |
| I3 | Secrets in configuration or repository | LiteLLM master key, AWS identifiers | MEDIUM | Key generated on the host into a `chmod 600` file excluded from version control; this repository contains placeholders only. Bedrock, OpenSearch and OSIS all use IAM roles, no long-lived keys |
| I4 | Dashboards exposing customer identifiers | OpenSearch UI | MEDIUM | Sample queries use opaque customer ids, never e-mail or names; workspace collaborators are managed explicitly |

### 4.5 Denial of service

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| D1 | Telemetry burst overwhelming ingestion | OpenSearch Ingestion | LOW | Pipeline `min_units`/`max_units` bound throughput; the Collector batches and bounds its sending queue |
| D2 | Runaway agent loop driving Bedrock cost | Assistant / Bedrock | MEDIUM | The demo's feature flags can simulate this; monitor `gen_ai.usage.*` per conversation and alarm on tool-call counts; set Bedrock service quotas and AWS Budgets |
| D3 | Evaluation job cost growth | Evaluation job / Bedrock | LOW | Scores each conversation once (deduplicated by trace id); cost per conversation is stored and visible on the dashboard; use sampling for high traffic |
| D4 | Collection compute scale-up | OpenSearch Serverless | LOW | Collection group `max*CapacityInOCU` caps spend |

### 4.6 Elevation of privilege

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| E1 | Over-permissioned pipeline role | IAM | MEDIUM | Role limited to `aoss:APIAccessAll` on the collection and `s3:PutObject` on the dead-letter prefix; see `iam/pipeline-role.json` |
| E2 | Over-permissioned host role | IAM | MEDIUM | `bedrock:InvokeModel*` on named inference profiles and `osis:Ingest` on the pipeline ARN only; see `iam/host-role.json`. Add collection data access only if the evaluation job runs on the host |
| E3 | Container escape on the host | Docker | LOW | Stock OpenTelemetry Demo images; host has no secrets beyond the LiteLLM key; instance role scoped as above |

## 5. Security controls summary

| Control | Status | Details |
|---|---|---|
| Encryption in transit | Enabled | HTTPS/SigV4 to OpenSearch Ingestion, OpenSearch Serverless, Bedrock; Collector-to-OSIS over TLS |
| Encryption at rest | Enabled | OpenSearch Serverless (AWS-owned key via the encryption policy); EBS root volume created with `Encrypted: true`; S3 default encryption on the DLQ bucket |
| Authentication | IAM only | No basic auth or API keys for AWS services; LiteLLM master key is local to the host |
| Least privilege | Scoped | Example policies in `iam/` |
| Network exposure | None inbound | Shop UI via SSM port forward; LiteLLM loopback only |
| Logging | Enabled | OpenSearch Ingestion pipeline logs; CloudTrail; SSM session logging recommended |
| Data retention | Configured | `index.data.retention` in every index template |

## 6. Before using with real traffic

1. Redact or omit message content and tool arguments in the Collector, or complete a data-protection review (I1).
2. Restrict the collection's network policy to a VPC endpoint (S2).
3. Run the evaluation job under its own IAM role with write access to the evaluation index only (T3).
## 7. Residual risk

| Category | Level for this sample | Notes |
|---|---|---|
| Authentication / authorization | LOW | IAM everywhere, scoped policies |
| Data protection | MEDIUM | Full conversation content is recorded by design; acceptable for synthetic data, requires review for real users |
| Availability / cost | LOW | Bounded by pipeline units, OCU caps and per-conversation evaluation |
| Network | LOW | No inbound exposure; public collection endpoint by default |

## 8. Document information

- Version 1.0, November 2026
- Review after significant changes to the architecture or data handling
