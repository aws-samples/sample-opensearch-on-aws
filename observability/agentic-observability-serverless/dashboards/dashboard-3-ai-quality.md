# Dashboard 3 · AI assistant: quality

Dataset: `agent-evals` (time field `@timestamp`), written by `../evaluation/score_live.py`.

Header: `# Astronomy Shop · AI assistant: quality` / `##### Every conversation evaluated against what the tools returned, last 24 hours`

## Tiles

**Conversations evaluated**
```
source = agent-evals | stats count() as scored
```
**Average score (1–5)**
```
source = agent-evals | stats avg(score_overall) as a | eval average_score = round(a, 2) | fields average_score
```
**Pass rate (%)**
```
source = agent-evals | eval p = if(label = 'pass', 1, 0) | stats sum(p) as passed, count() as total | eval pass_rate_pct = round(passed * 100.0 / total, 1) | fields pass_rate_pct
```
**Cost of evaluation ($)**
```
source = agent-evals | stats sum(judge_cost_usd) as j | eval eval_spend_usd = round(j, 2) | fields eval_spend_usd
```
**Hallucinations**
```
source = agent-evals | where hallucination = true | stats count() as hallucinations
```

## Charts

**Conversations needing review by model (%)** (bar, red)
```
source = agent-evals | eval f = if(label = 'fail', 1, 0) | stats sum(f) as fails, count() as total by model | eval `Fail rate (%)` = round(fails * 100.0 / total, 1) | fields model, `Fail rate (%)`
```
**Hallucinations by model and use case** (bar)
```
source = agent-evals | where hallucination = true | stats count() as hallucinations by model, use_case | sort - hallucinations
```
**Quality by model** (table)
```
source = agent-evals | stats count() as Conversations, avg(score_overall) as o, avg(score_correct) as c, avg(score_helpful) as h, avg(cost_usd) as cost by model | rename model as Model | eval `Overall (1-5)` = round(o, 2), `Correct (1-5)` = round(c, 2), `Helpful (1-5)` = round(h, 2), `Cost per conversation ($)` = round(cost, 4) | fields Model, Conversations, `Overall (1-5)`, `Correct (1-5)`, `Helpful (1-5)`, `Cost per conversation ($)`
```
**Quality and cost by use case and model** (table; the decision panel)
```
source = agent-evals | stats count() as Conversations, avg(score_overall) as o, avg(cost_usd) as cost by use_case, model | rename use_case as `Use case`, model as Model | eval `Overall (1-5)` = round(o, 2), `Cost per conversation ($)` = round(cost, 4) | fields `Use case`, Model, Conversations, `Overall (1-5)`, `Cost per conversation ($)` | sort `Use case`, Model
```
**Lowest-scoring conversations** (table; `traceId` is the link into Agent Traces)
```
source = agent-evals | where score_overall < 3.5 | fields @timestamp, model, use_case, score_overall, hallucination, question, explanation, traceId | sort score_overall | head 10
```
**Average score per hour by model** (line; Y axis 1–5)
```
source = agent-evals | stats avg(score_overall) as average_score by span(@timestamp, 1h), model
```

## From a score to the trace

Agent Traces, query bar (plain language works too: "find me this trace id"):
```
| where traceId = '<traceId from the table>'
```
Open the trace to see the agent, model and tool spans for that conversation.
