# Threat Model: Agentic observability with the Amazon OpenSearch Service optimized engine

## 1. System overview

This sample runs the OpenTelemetry Demo (an e-commerce application with an AI shopping agent) on an Amazon EC2
instance, sends its traces and logs through Amazon OpenSearch Ingestion into an Amazon OpenSearch Service domain that
uses the Optimized engine, and explores them in OpenSearch UI.

Components:

- EC2 instance (Amazon Linux 2023, SSM-managed, no inbound ports) running Docker Compose: the OpenTelemetry Demo
  services, the LangGraph shopping agent, a LiteLLM proxy, and the OpenTelemetry Collector
- Amazon Bedrock (Anthropic Claude models) called by the agent through LiteLLM, using the instance role
- Amazon OpenSearch Ingestion pipeline (OTLP source, SigV4-authenticated) with a dead-letter queue in Amazon S3
- Amazon OpenSearch Service domain, Optimized engine, fine-grained access control with IAM
- OpenSearch UI application and Observability workspace
- Amazon S3 bucket holding the rendered demo overlay read by the instance at boot
- Operator workstation running the AWS CLI, `awscurl`, and the scripts in this sample

## 2. Data flow

```
[Load generator] -> [Demo services + AI agent] -> [LiteLLM, loopback] -> [Amazon Bedrock]
                            | OTLP
                            v
                   [OTel Collector] -- OTLP/HTTP + SigV4 --> [OpenSearch Ingestion] --> [OpenSearch Service domain]
                                                                     |                            ^
                                                                     v                            |
                                                              [S3 dead-letter queue]         [OpenSearch UI]
[Operator] -- SSM Run Command / Session Manager --> [EC2 host]
[Operator] -- SigV4 (awscurl) --> [domain: templates, role mapping]
[EC2 host, at boot] -- SigV4 --> [S3 config bucket: demo overlay]
```

## 3. Trust boundaries

| Boundary | Components | Trust level |
|---|---|---|
| Internet | Nothing exposed inbound. The pipeline's ingest endpoint and the domain endpoint are public but require SigV4 | Untrusted |
| EC2 host | Demo containers, LiteLLM (loopback only), Collector | Semi-trusted (synthetic traffic, instance role) |
| AWS services | Bedrock, OpenSearch Ingestion, OpenSearch Service, OpenSearch UI, IAM, S3, CloudWatch | Trusted |
| Operator workstation | AWS CLI, scripts, browser | Trusted (IAM identity) |

## 4. Threat analysis (STRIDE)

### 4.1 Spoofing

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| S1 | Unauthenticated telemetry sent to the ingest endpoint | OpenSearch Ingestion | MEDIUM | The endpoint requires SigV4 with `osis:Ingest`; unsigned requests are rejected with 401 and logged. The host role grants `osis:Ingest` on this pipeline's ARN only |
| S2 | Unauthorized access to the domain | OpenSearch Service | HIGH | Fine-grained access control with IAM: the master user is the operator's identity; `all_access` is mapped to the pipeline role and the operator only. The domain access policy allows the account's principals, so IAM still decides. For production, use a VPC endpoint |
| S3 | Calling the LiteLLM proxy from outside the host | LiteLLM | LOW | Bound to `127.0.0.1` and the Docker network; a master key in a `chmod 600` file; Bedrock credentials come from the instance role, never from the proxy's clients |
| S4 | Access to the EC2 host | EC2 | MEDIUM | No inbound security-group rules; SSM Session Manager and Run Command only (IAM-authenticated, logged in CloudTrail) |

### 4.2 Tampering

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| T1 | Modified pipeline or template configuration | OpenSearch Ingestion, domain | LOW | IAM controls `osis:UpdatePipeline` and the domain's index APIs; configuration is kept in version control and rendered from `config.env` |
| T2 | Malformed telemetry altering stored data or breaking queries | Collector, pipeline | LOW | Only the host role can sign ingest requests. Index templates fix every field type before the first document arrives; a document that does not fit is written to the dead-letter queue with the reason, and the index is unchanged |
| T3 | Tampered demo overlay delivered to the host at boot | S3 config bucket | MEDIUM | The bucket is private and tagged by the sample; the host role has `s3:GetObject` on the one overlay object only. For production, add a bucket policy that limits `PutObject` to the operator and enable versioning |
| T4 | Prompt injection through shopper input reaching the agent's tools | Demo agent | LOW in this sample | Traffic is synthetic. The agent's tools are read-only catalog calls and a demo checkout. The sample stores prompts and answers as text; see I1 before doing that with real users |

### 4.3 Repudiation

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| R1 | Untracked configuration or data changes | All AWS services | LOW | CloudTrail records control-plane calls; the pipeline logs to CloudWatch; the domain can publish audit logs; SSM session logging is recommended for the host |
| R2 | Rejected telemetry disappearing without trace | Pipeline | LOW | Every sink has a dead-letter queue in S3 with the rejected document and the error; the `documentErrors` metric is the signal to alarm on |

### 4.4 Information disclosure

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| I1 | Personal data in traces: prompts, model answers, tool arguments, customer details | Spans, logs, dead-letter bucket | HIGH for production use | The demo uses synthetic shoppers only. The Collector runs the demo's redaction processors (e-mail hashed, card number masked). For real traffic, do not record `gen_ai.input.messages`, `gen_ai.output.messages`, or tool arguments without a data-protection review; restrict read access with fine-grained access control roles; set an index lifecycle (ISM) and a lifecycle rule on the dead-letter bucket |
| I2 | Conversation content sent to the model | Amazon Bedrock | MEDIUM | Bedrock does not use customer content to train models and keeps data in-Region. Still subject to I1 review |
| I3 | Secrets in configuration or the repository | LiteLLM master key, AWS identifiers | MEDIUM | The repository contains placeholders only; `config.env` and `rendered/` are git-ignored. On the host the key files are `chmod 600`. Bedrock, OpenSearch, and OpenSearch Ingestion use IAM roles, no long-lived keys |
| I4 | Query results exposing identifiers in OpenSearch UI | OpenSearch UI | MEDIUM | The sample queries show service names, operations, and token counts, not shopper identities; workspace collaborators are managed explicitly |

### 4.5 Denial of service

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| D1 | Telemetry volume exceeding pipeline or domain capacity | OpenSearch Ingestion, domain | LOW | The pipeline scales between 1 and 4 OCUs; the Collector applies back-pressure and retries; the domain's two data nodes are sized for the demo's load. Alarm on `documentErrors` and on the domain's `ClusterIndexWritesBlocked` |
| D2 | Runaway Bedrock spend from the load generator | Amazon Bedrock | MEDIUM | `LOCUST_USERS=2` keeps the agent at a few calls per minute; the host role is limited to two inference profiles. Set an AWS Budgets alert and stop the instance when not in use; `scripts/cleanup.sh` terminates it |
| D3 | Unsigned probes flooding the public ingest endpoint | OpenSearch Ingestion | LOW | Rejected at authentication; they appear as 401 log lines and do not consume pipeline units |

### 4.6 Elevation of privilege

| ID | Threat | Component | Risk | Mitigation |
|---|---|---|---|---|
| E1 | Container on the host using the instance role for other actions | EC2, IAM | MEDIUM | The host role allows `osis:Ingest` and `osis:GetPipeline` on one pipeline, `bedrock:InvokeModel*` on two inference profiles, `s3:GetObject` on one object, and the SSM managed policy. IMDSv2 is required; the hop limit of 2 is the minimum that lets containers reach the metadata service |
| E2 | Pipeline role used beyond its sinks | IAM | LOW | The pipeline role is assumable by `osis-pipelines.amazonaws.com` only and allows `es:ESHttp*` on this domain and `s3:PutObject` on the dead-letter bucket |
| E3 | Operator locked out or over-privileged by the `all_access` mapping | Domain security plugin | MEDIUM | The mapping file keeps the operator's ARN and adds the pipeline role; a PUT that omits the operator removes their access. For production, map the pipeline role to a role limited to the OpenTelemetry indices instead of `all_access` |

## 5. Assumptions

- The sample is deployed in a development account with synthetic traffic.
- The operator's IAM identity is trusted and is the domain's master user.
- Public endpoints with SigV4 are acceptable for the sample; production deployments use VPC endpoints.

## 6. Recommendations for production use

1. Place the domain and the pipeline behind VPC endpoints.
2. Replace the `all_access` mapping for the pipeline role with a role limited to `otel-*` and `logs-otel-*` indices.
3. Review which GenAI attributes are stored (I1) and apply ISM retention to the span and log indices.
4. Add a bucket policy and versioning on the config bucket; add a lifecycle rule on the dead-letter bucket.
5. Alarm on the pipeline's `documentErrors` and the domain's write-block and storage metrics.
6. Set an AWS Budgets alert for Bedrock usage.

Threat model version: 1.0 (November 2026).
