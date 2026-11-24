# Dashboard 2 · AI assistant: cost

Dataset: the span index. Prices in the queries are AWS list prices for us-east-1 (Claude Haiku 5.5 $0.11 / $0.55,
Claude Sonnet 5.5 $2.20 / $11.00 per 1M input / output tokens, November 2026); adjust for your models and region.
Spend is computed from token counts on the spans, not from billing.

Header: `# Astronomy Shop · AI assistant: cost` / `##### Conversations, tokens, models and dollars, last 24 hours`

The cost expression used below:
```
| eval in_m = `gen_ai.usage.input_tokens` / 1000000.0, out_m = `gen_ai.usage.output_tokens` / 1000000.0
| eval usd = case(like(`attributes.gen_ai.response.model`, '%haiku%'), in_m * 0.11 + out_m * 0.55, like(`attributes.gen_ai.response.model`, '%sonnet%'), in_m * 2.20 + out_m * 11.00 else 0.0)
```

## Tiles

**Conversations**
```
source = otel-v1-apm-span-* | where name = 'astronomy_shop_agent_workflow.workflow' | stats count() as conversations
```
**Model calls**
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'chat' | stats count() as model_calls
```
**Tokens (M)**
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'chat' | stats sum(`gen_ai.usage.input_tokens`) as i, sum(`gen_ai.usage.output_tokens`) as o | eval tokens_m = round((i + o) / 1000000.0, 2) | fields tokens_m
```
**Spend ($)**
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'chat' | eval in_m = `gen_ai.usage.input_tokens` / 1000000.0, out_m = `gen_ai.usage.output_tokens` / 1000000.0 | eval usd = case(like(`attributes.gen_ai.response.model`, '%haiku%'), in_m * 0.11 + out_m * 0.55, like(`attributes.gen_ai.response.model`, '%sonnet%'), in_m * 2.20 + out_m * 11.00 else 0.0) | stats sum(usd) as s | eval spend_usd = round(s, 2) | fields spend_usd
```
**Cost per conversation ($)**
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'chat' | eval in_m = `gen_ai.usage.input_tokens` / 1000000.0, out_m = `gen_ai.usage.output_tokens` / 1000000.0 | eval usd = case(like(`attributes.gen_ai.response.model`, '%haiku%'), in_m * 0.11 + out_m * 0.55, like(`attributes.gen_ai.response.model`, '%sonnet%'), in_m * 2.20 + out_m * 11.00 else 0.0) | stats sum(usd) as s, distinct_count(traceId) as c | eval cost_per_conversation = round(s / c, 4) | fields cost_per_conversation
```

## Charts

**Conversations per hour by model** (stacked bar)
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'chat' | stats distinct_count(traceId) as conversations by span(startTime, 1h), `attributes.gen_ai.response.model`
```
**Share of spend by model** (donut)
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'chat' | eval in_m = `gen_ai.usage.input_tokens` / 1000000.0, out_m = `gen_ai.usage.output_tokens` / 1000000.0 | eval usd = case(like(`attributes.gen_ai.response.model`, '%haiku%'), in_m * 0.11 + out_m * 0.55, like(`attributes.gen_ai.response.model`, '%sonnet%'), in_m * 2.20 + out_m * 11.00 else 0.0) | stats sum(usd) as spend_usd by `attributes.gen_ai.response.model`
```
**What shoppers ask** (donut)
```
source = otel-v1-apm-span-* | where name = 'user_ask_agent' | eval `Use case` = case(`attributes.demo.agent.use_case` = 'product_search', 'Product search', `attributes.demo.agent.use_case` = 'promotions', 'Promotions', `attributes.demo.agent.use_case` = 'shipping_currency', 'Shipping & currency', `attributes.demo.agent.use_case` = 'buy', 'Buy' else 'Other') | stats count() as conversations by `Use case`
```
**Cost by model** (table)
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'chat' | eval in_m = `gen_ai.usage.input_tokens` / 1000000.0, out_m = `gen_ai.usage.output_tokens` / 1000000.0 | eval usd = case(like(`attributes.gen_ai.response.model`, '%haiku%'), in_m * 0.11 + out_m * 0.55, like(`attributes.gen_ai.response.model`, '%sonnet%'), in_m * 2.20 + out_m * 11.00 else 0.0) | stats distinct_count(traceId) as Conversations, sum(`gen_ai.usage.input_tokens`) as i, sum(`gen_ai.usage.output_tokens`) as o, sum(usd) as s by `attributes.gen_ai.response.model` | rename `attributes.gen_ai.response.model` as Model | eval `Tokens per conversation` = round((i + o) / Conversations, 0), `Spend ($)` = round(s, 2), `Cost per conversation ($)` = round(s / Conversations, 4) | fields Model, Conversations, `Tokens per conversation`, `Spend ($)`, `Cost per conversation ($)`
```
**Spend per hour ($)** (line)
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'chat' | eval in_m = `gen_ai.usage.input_tokens` / 1000000.0, out_m = `gen_ai.usage.output_tokens` / 1000000.0 | eval usd = case(like(`attributes.gen_ai.response.model`, '%haiku%'), in_m * 0.11 + out_m * 0.55, like(`attributes.gen_ai.response.model`, '%sonnet%'), in_m * 2.20 + out_m * 11.00 else 0.0) | stats sum(usd) as spend_usd by span(startTime, 1h)
```
**Tokens per hour: input vs output** (stacked bar, two Y fields). Input dominates because every model call re-sends
the system prompt, tool definitions and tool results.
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'chat' | stats sum(`gen_ai.usage.input_tokens`) as input_tokens, sum(`gen_ai.usage.output_tokens`) as output_tokens by span(startTime, 1h)
```
**Model response time** (table)
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'chat' | stats count() as Calls, percentile_approx(durationInNanos, 50) as p50, percentile_approx(durationInNanos, 95) as p95 by `attributes.gen_ai.response.model` | rename `attributes.gen_ai.response.model` as Model | eval `Typical (p50) s` = round(p50 / 1000000000.0, 1), `Tail (p95) s` = round(p95 / 1000000000.0, 1) | fields Model, Calls, `Typical (p50) s`, `Tail (p95) s`
```
**Tools the assistant uses** (bar)
```
source = otel-v1-apm-span-* | where `gen_ai.operation.name` = 'execute_tool' | stats count() as calls by `gen_ai.tool.name` | sort - calls
```
