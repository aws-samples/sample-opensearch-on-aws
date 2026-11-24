# Query Workbench reports

Query Workbench has no time picker, so the window is in the query (`timestampadd(HOUR, -24, now())`).

## Assistant checkouts by outcome

The checkout tool returns its failure as text, with span status 0 (unset), so filter on the result content.
```
source = otel-v1-apm-span-* | where startTime > timestampadd(HOUR, -24, now()) and `gen_ai.operation.name` = 'execute_tool' and `gen_ai.tool.name` = 'checkout' | eval outcome = if(like(`gen_ai.tool.call.result`, '%Checkout failed%'), 'Failed', 'Succeeded') | stats count() as checkouts by outcome
```

## Failed purchases per hour
```
source = otel-v1-apm-span-* | where startTime > timestampadd(HOUR, -24, now()) and `gen_ai.operation.name` = 'execute_tool' and `gen_ai.tool.name` = 'checkout' and like(`gen_ai.tool.call.result`, '%Checkout failed%') | stats count() as failed_checkouts by span(startTime, 1h)
```

## Affected customers (no PII)

The customer id is an opaque UUID the shopper supplied in the conversation. PPL `parse` requires a full-string
match, hence the `(?s).* ... .*` wrapper; capture-group names may not contain underscores.
```
source = otel-v1-apm-span-* | where startTime > timestampadd(HOUR, -24, now()) and name = 'astronomy_shop_agent_workflow.workflow' and like(`gen_ai.output.messages`, '%Checkout failed%') | parse `gen_ai.input.messages` '(?s).*user id (?<customer>[0-9a-f-]+).*' | rename startTime as `Time`, customer as `Customer id`, traceId as `Trace id` | fields `Time`, `Customer id`, `Trace id` | sort - `Time`
```
Headline numbers:
```
source = otel-v1-apm-span-* | where startTime > timestampadd(HOUR, -24, now()) and name = 'astronomy_shop_agent_workflow.workflow' and like(`gen_ai.output.messages`, '%Checkout failed%') | parse `gen_ai.input.messages` '(?s).*user id (?<customer>[0-9a-f-]+).*' | stats distinct_count(customer) as `Customers affected`, count() as `Failed purchases`, min(startTime) as `First seen`, max(startTime) as `Last seen`
```

## Failed "buy" conversations with the evaluation's reason
```
source = agent-evals | where `@timestamp` > timestampadd(HOUR, -24, now()) and use_case = 'buy' and label = 'fail' | fields `@timestamp`, model, score_overall, hallucination, cost_usd, duration_ms, explanation, traceId | sort score_overall
```
