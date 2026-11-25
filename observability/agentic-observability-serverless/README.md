# Agentic observability on Amazon OpenSearch Serverless

An end-to-end sample for observing and evaluating an AI shopping assistant with Amazon OpenSearch Serverless. The
sample uses the OpenTelemetry Demo e-commerce application to collect traces and logs through Amazon OpenSearch
Ingestion into a next-generation OpenSearch Serverless time series collection.

Follow the deployment scripts and recipes to build OpenSearch UI dashboards for business health, AI cost and AI
quality, and run an online evaluation job that scores each conversation against what the assistant's tools returned.
Adapt these patterns to your own instrumented applications and agents.

The diagram below shows the data flow, left to right: the OpenTelemetry Demo (signal producer) emits traces and logs
through its OpenTelemetry Collector to an Amazon OpenSearch Ingestion pipeline, which transforms them and writes to an
Amazon OpenSearch Serverless time series collection, which OpenSearch UI reads. Above the main row, Amazon Bedrock
serves the assistant's models and the evaluation model; the evaluation job reads conversations from the collection
and writes scores back to it. Below the row, Amazon Managed Service for Prometheus is shown as an optional metrics
store that OpenSearch UI can also read (not used in this sample).

![Architecture: OpenTelemetry Demo, OpenTelemetry Collector, Amazon OpenSearch Ingestion, Amazon OpenSearch Serverless and OpenSearch UI in a row; Amazon Bedrock above, linked to the demo (agent models) and to the collection (online evaluation); Amazon Managed Prometheus below as optional](docs/images/architecture.png)

## What the sample shows

The Astronomy Shop is an online store with twenty microservices and an AI shopping assistant that answers
questions ("What's the cheapest telescope?", "Any promotions on binoculars?", "What does shipping to Sydney cost?")
and can place orders. Everything it does is OpenTelemetry data in one OpenSearch Serverless collection, viewed
through three OpenSearch UI dashboards:

1. **Shop health & business**: availability, latency, failed requests, orders placed and order value (from an
   attribute on the checkout span), and the number of assistant conversations. The usual board; it says the shop is
   healthy, and nothing on it says whether the assistant's answers were right.
2. **AI assistant: cost**: conversations, model calls, tokens, spend at list prices, cost per conversation, which
   tools the assistant calls, response time. The assistant runs on two models (Claude Haiku 5.5 for 70% of
   conversations, Claude Sonnet 5.5 for 30%); in a test run Sonnet accounted for about nine tenths of the spend.
3. **AI assistant: quality**: the output of online evaluation. Every ten minutes an evaluation job takes each new
   conversation from the trace data (the shopper's question, the assistant's final answer, and the raw results its
   tools returned), sends them to an evaluation model on Amazon Bedrock with a fixed rubric (correct, relevant,
   helpful, 1–5; hallucination flag), and writes the score next to the trace.

What the evaluation found in a test run: both models scored within 0.1 of each other (about 4.5 / 5), at a twentieth
of the cost per conversation for Haiku. Every low-scoring conversation was a "buy" request: the shop's checkout
service returned HTTP 500, the assistant's checkout tool reported Success (the function completed and returned the
error as text), the assistant told the shopper it was rebuilding the cart, and the HTTP request returned 200 OK.
No error-rate panel showed it; the evaluation did. From the score, Agent Traces opens the conversation's trace down
to the failing span, and one PPL query in Query Workbench lists every affected conversation of the day.

| Layer | What we used | Notes |
|---|---|---|
| Application | [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) 3.1.0 with its agentic layer | 20 microservices, a LangGraph shopping assistant, load generator |
| Models | Amazon Bedrock: Claude Haiku 5.5 (70%), Claude Sonnet 5.5 (30%) via a LiteLLM proxy | instance-role credentials, no keys |
| Collection | OTel Collector (in the OpenTelemetry Demo) → OTLP/HTTP with SigV4 | one Collector pipeline per OSIS exporter |
| Ingestion | Amazon OpenSearch Ingestion, `otlp` source, `otel_traces` processor, `custom` index type | Serverless sinks use `index_type: custom`; the mapping comes from `pipeline/index-templates/` |
| Storage | Amazon OpenSearch Serverless, time series collection (next generation) | scales to zero; every document needs the index's timestamp field |
| Evaluation | Python job calling Bedrock, writing scores next to the traces | every 10 minutes, ~1 cent per conversation |
| UI | OpenSearch UI: Dashboards, Agent Traces, Discover, Query Workbench, Ask AI | plain-language questions become PPL |

## Build order

Everything is created with the scripts in `deploy/` (Python, boto3; `pip install -r deploy/requirements.txt`).
Pick a name prefix (`agentobs` below) and a globally unique S3 bucket name for the pipeline's dead-letter queue
(for example `<prefix>-dlq-<account-id>`). The scripts use us-east-1 unless you pass `--region`; they do not read
your AWS CLI's default region. Each script is idempotent and prints the identifiers the next one needs.

0. **Confirm the plan** (`deploy/00_preflight.py`, read-only, under a minute): a short summary of the account,
   region, what will be created, models, versions and estimated cost. It creates nothing. It asks which IAM user or
   role you sign in to the AWS console with (it goes into `--principals`) and whether to go ahead.

> **Coding agents:** run `deploy/00_preflight.py` first and pass its summary to the user as it is, in a few
> lines. Ask its two questions (console sign-in identity, go ahead) and wait for "yes" before creating anything.
> Ask again before running `deploy/99_cleanup.py`. Run the evaluation once with a small `--limit`; do not leave
> `--loop` running on the user's machine.

1. **Collection** (`deploy/01_collection.py`, ~5 min): next-generation collection group, time series collection,
   encryption, network and data access policies. Pass with `--principals` both the identity that runs the scripts and
   the IAM user or role you sign in to the AWS console with (for a role, its role ARN). OpenSearch UI shows no data to
   anyone missing from this list. The default scales search to zero when idle; set `--min-search-ocu 2` (the smallest non-zero value the service accepts) for production, mission-critical or always-on demo workloads where the first query after an idle period must be fast.
2. **Pipeline** (`deploy/02_pipeline.py`, ~10 min): pipeline IAM role, DLQ bucket, the index templates from
   `pipeline/index-templates/`, and the OpenSearch Ingestion pipeline from `pipeline/pipeline.yaml` with CloudWatch
   logging. Run it as one of the principals from step 1 (it applies the index templates through the collection
   endpoint). `pipeline/README.md` explains each processor. Wait a few minutes after ACTIVE before sending data.
3. **Application** (`deploy/03_host.py`, ~25 min until spans arrive; add `--demo-app-s3 s3://<dlq-bucket>/demo-app` if
   the script reports the user data over the 16 KiB limit): host IAM role, security group and an EC2
   instance whose user data clones the OpenTelemetry Demo at tag 3.1.0, applies the additions in `demo-app/`
   (live models on Bedrock, weighted model mix, realistic shopper questions), points the Collector at the pipeline
   and starts the stack with the demo's own `make start-agentic`. `demo-app/README.md` describes the additions. If RunInstances reports InsufficientInstanceCapacity, re-run with a public subnet in another Availability Zone or another 8 vCPU / 32 GiB type via --instance-type (for example r7a.2xlarge); the role and security group are reused.
4. **OpenSearch UI** (`deploy/04_ui.md`, ~15 min): application, workspace, datasets, and a query to confirm data is
   arriving. Then build dashboards 1 and 2 from `dashboards/dashboard-1-health.md` and `dashboard-2-ai-cost.md`.
5. **Online evaluation** (`evaluation/README.md`): run `score_live.py` once from your workstation with a small
   `--limit` to check scoring works, then build dashboard 3 from `dashboards/dashboard-3-ai-quality.md`. Each scored
   conversation costs about 1 cent in Bedrock calls; do not leave `--loop` running on a laptop (see
   `evaluation/README.md`, "Continuous scoring").
6. **Investigate** (`dashboards/queries/failed-checkouts.md`): Query Workbench reports for assistant checkouts that
   failed and the conversations behind them.

The OpenSearch Observability Stack installer (`npx @opensearch-project/observability-stack --serverless`,
[docs](https://observability.opensearch.org/docs/deploy/aws/)) can create a comparable base stack with its own
names and templates; the scripts here create exactly what the rest of the sample expects.
## Notes for running it

- Give each OpenSearch Ingestion exporter its own Collector pipeline with a bounded `sending_queue`.
- Restart the `agent` container after any Collector or pipeline change; its OTel exporter does not resume on its own
  after a period of unreachability.
- The evaluation job needs 1,000+ output tokens for the evaluation model; see `evaluation/README.md`.
## Prerequisites

- An AWS account with access to Amazon OpenSearch Serverless (next-generation time series collections), Amazon
  OpenSearch Ingestion, OpenSearch UI and Amazon Bedrock in one region. The scripts default to us-east-1 and the
  `us.` Bedrock inference profiles for Claude Haiku 5.5 and Claude Sonnet 5.5. In another region, pass `--region`
  and `--model-prefix` (for example `eu`, `apac` or `global`) to `00_preflight.py` and `03_host.py`, and set
  `MODEL_PREFIX` for the evaluation job; `00_preflight.py` reports whether the profiles exist there. Only us-east-1
  has been tested.
- Bedrock model access granted for Anthropic Claude Haiku 5.5 and Claude Sonnet 5.5
- An EC2 instance able to run the OpenTelemetry Demo (8 vCPU / 32 GiB recommended, Amazon Linux 2023, Docker,
  SSM agent, no inbound security-group rules)
- Workstation: AWS CLI v2, Python 3.11+. Optional: the Session Manager plugin, only to open the shop's web page or a
  shell on the host (`deploy/03_host.py` prints the command); the deployment itself does not need it.

## Cost

Approximate, us-east-1, at the load generator's default rate (two simulated users), November 2026 list prices:

| Item | Cost |
|---|---|
| EC2 r8a.2xlarge | ~USD 11/day |
| OpenSearch Serverless time series collection, group min 0 / max 2 OCU | compute billed per OCU-hour while active; a few USD/day at this volume |
| OpenSearch Ingestion pipeline, 1–2 units | ~USD 6–12/day |
| Bedrock, assistant (70/30 Haiku/Sonnet) | ~USD 8–10/day |
| Bedrock, evaluation (Sonnet, ~3,000 conversations/day) | ~USD 30/day; use sampling to reduce |
| OpenSearch UI application | no charge |

## Cleanup

```bash
python deploy/99_cleanup.py --name agentobs --region us-east-1 --dlq-bucket <dlq-bucket>
```
Deletes, in reverse order, what the deploy scripts created: the EC2 instance, security group and host role; the
OpenSearch UI application; the pipeline and its log group; the pipeline role; the collection, its policies and the
collection group. Each resource is deleted only if it carries the tag `project=agentic-observability-serverless`
that the deploy scripts set; anything else with a matching name is listed and left alone. In the DLQ bucket only
objects under `dlq/` are removed (and under `--demo-app-s3`, if you used it); the bucket itself is deleted only if
the sample created it and it is then empty. Stop the evaluation job first. The workspace and dashboards live inside
the application and go with it.
## Repository layout

```
deploy/              preflight check, creation scripts (collection, pipeline, host), OpenSearch UI steps, cleanup script
pipeline/            OSIS pipeline YAML, index templates, Collector exporter snippet
demo-app/            compose overlays, LiteLLM config, agent and load-generator patches, env example, NOTICE
evaluation/          score_live.py (online evaluation job), check_evals.py, parser test, requirements, README
dashboards/          three dashboards as panel lists with PPL; Query Workbench reports
iam/                 least-privilege examples for the pipeline role and the EC2 host role
docs/images/         architecture diagram
THREAT_MODEL.md      STRIDE threat model for the provisioned resources
```

Placeholders to replace: `<account-id>`, `<region>`, `<collection-id>`, `<collection-name>`, `<pipeline-name>`,
`<pipeline-role>`, `<prefix>`, `<dlq-bucket>`, `<model-prefix>` (Bedrock inference profile prefix, `us` by
default). The `deploy/` scripts fill them in; `<prefix>` is the name prefix (also the path segment in the pipeline URL, e.g. `https://<endpoint>/agentobs/v1/traces`).

## Further reading

- [AI observability in Amazon OpenSearch Service](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/observability-ai.html)
- [Time series collections for the next generation of OpenSearch Serverless](https://aws.amazon.com/blogs/big-data/introducing-the-next-generation-of-serverless-time-series-for-amazon-opensearch-service/)

## Security

See `THREAT_MODEL.md`. The sample records full conversation content in traces and uses synthetic shoppers only.

## License

This sample is part of [aws-samples/sample-opensearch-on-aws](https://github.com/aws-samples/sample-opensearch-on-aws)
and is licensed under MIT-0 (see the repository `LICENSE`). Modified files from the OpenTelemetry Demo remain under
Apache-2.0; see `demo-app/NOTICE`.
