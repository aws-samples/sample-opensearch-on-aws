# Demo application: OpenTelemetry Demo + live models on Amazon Bedrock

The application is the stock [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) (Astronomy
Shop) started with its agentic layer (`make start-agentic`: the LangGraph shopping assistant, chatbot UI and
load generator). Three small additions make the telemetry worth looking at:

| Addition | File | Why |
|---|---|---|
| Live models instead of recorded replies | `compose.bedrock.yaml`, `litellm/config.yaml`, `env.override.example` | The stock agent replays cassettes (`USE_VCR=True`) and records no token usage. A LiteLLM proxy gives the agent an OpenAI-compatible endpoint backed by Bedrock; the EC2 instance role supplies credentials, no keys. |
| Weighted model mix with correct names | `patches/agent/llm.py` | 70% Claude Haiku 5.5 / 30% Claude Sonnet 5.5, chosen per conversation inside the agent, so `gen_ai.response.model` on each span names the model that actually answered. (A weighted alias in LiteLLM would label every span with the alias.) |
| Realistic shopper questions | `patches/loadgen/agent_prompts.py`, `patches/loadgen/locustfile.py` | 13 questions in four use cases (product search, promotions, shipping & currency, buy), each conversation tagged with `demo.agent.use_case` and a fake `user.id`. "Buy" questions carry a customer record so the agent exercises the full cart and checkout path. |

The patches are bind-mounted over the stock images (`compose.patches.yaml`); nothing is rebuilt.

## Setup on the host

`deploy/03_host.py` does all of this in the instance's user data. By hand, on an Amazon Linux 2023 host with Docker:

```bash
git clone --depth 1 --branch 3.1.0 https://github.com/open-telemetry/opentelemetry-demo && cd opentelemetry-demo
cp -r <this-repo>/demo-app/{compose.bedrock.yaml,compose.patches.yaml,litellm,patches} .
cp <this-repo>/demo-app/env.override.example .env.override
python3 -c 'import secrets; print("LITELLM_MASTER_KEY=sk-" + secrets.token_hex(32))' > litellm/litellm.env && chmod 600 litellm/litellm.env
sed -i "s|^API_KEY=.*|API_KEY=$(sed -n 's/^LITELLM_MASTER_KEY=//p' litellm/litellm.env)|" .env.override
# Collector: write src/otel-collector/otelcol-config-extras.yml (see ../pipeline/README.md, "Collector side")
# Start with the demo's own target, so both .env and .env.override are applied and the overlays load in the right order
sed -i 's|DOCKER_COMPOSE_FILES_AGENT=-f compose.agent.yaml|DOCKER_COMPOSE_FILES_AGENT=-f compose.agent.yaml -f compose.bedrock.yaml -f compose.patches.yaml|' Makefile
make start-agentic
```

Do not run `docker compose up` directly: Compose loads only `.env` by itself, and the demo keeps its overrides
(`USE_VCR`, `LLM_*`, `API_KEY`, `LOCUST_USERS`) in `.env.override`, which `make start-agentic` passes explicitly.
`compose.patches.yaml` forwards `LLM_MODEL_WEIGHTS` into the agent container; the stock compose file does not list it.

The Collector container signs requests to OpenSearch Ingestion with the instance role, read through IMDS; the
instance needs an IMDSv2 hop limit of 2 so a container can reach the metadata service. The pipeline endpoint comes
from `aws osis get-pipeline --pipeline-name <name>-otlp --query 'Pipeline.IngestEndpointUrls'`.
## IAM for the host role

`bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the inference profiles
`us.anthropic.claude-haiku-5-5` and `us.anthropic.claude-sonnet-5-5` and the underlying foundation models
(`../iam/host-role.json`). Request model access in the Bedrock console first.

## Operational notes

- The agent's OTel exporter stops with "Queue full, dropping Span" if the Collector is unreachable for a while and
  does not resume on its own. Restart the `agent` container after any Collector or pipeline change; a small watchdog
  that restarts it when no spans arrive is cheap insurance for an unattended demo.
- Token cost at the load generator's default rate (two users): roughly USD 3/day on Haiku only, 8–10/day with the
  70/30 mix, at list prices.
- LiteLLM does not pass `temperature` to Claude 5.5 models (the API rejects it); `drop_params: true` handles this.
