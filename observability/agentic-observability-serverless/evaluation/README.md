# Online evaluation of agent conversations

`score_live.py` reads finished conversations from the span index, asks an evaluation model on Amazon Bedrock to
score each one against what the agent's tools actually returned, and writes one document per conversation into the
`agent-evals` index, linked by `traceId`. The pattern follows the OpenSearch Observability Stack
[Evaluation & Scoring](https://observability.opensearch.org/docs/agent-evals/evaluation/) docs (list root spans,
skip the ones already scored, fetch the trace, score, write back). It writes straight to the collection rather
than through OTLP so it can run without an SDK-instrumented process.

## What one evaluation looks at

From the trace of a conversation:

| Evidence | Span | Field |
|---|---|---|
| The shopper's question | `user_ask_agent` (load generator) or the root agent span | `gen_ai.input.messages` |
| The assistant's final answer | last `ChatLLM.chat` span | `gen_ai.output.messages` |
| What each tool returned (catalogue, cart, shipping quote, checkout result) | `execute_tool *` spans | `gen_ai.tool.call.result` |
| Model, tokens, latency, use case | `ChatLLM.chat`, root, `user_ask_agent` | `gen_ai.response.model`, `gen_ai.usage.*`, `durationInNanos`, `attributes.demo.agent.use_case` |

The question, answer and tool results go to the evaluation model with a fixed rubric (`RUBRIC` in the script):

- **correct** 1–5: every fact in the answer is supported by the tool results.
- **relevant** 1–5: the answer addresses what was asked.
- **helpful** 1–5: the shopper can act on it.
- **hallucination** true/false: any fact the tools never returned, or a contradiction.
- **reason**: one sentence.

Derived: `score_overall` (mean of the three), `label` = `pass` if overall ≥ 3.5, no hallucination, a real answer
and no HTTP error, else `fail`. The evaluation model's own tokens and cost are stored on each document
(`judge_*`), so the cost of evaluating is itself observable.

**Adapt the rubric to your use case.** These rules fit a shop.

## Run it

Prerequisites: Python 3.11+, `pip install boto3 opensearch-py`; an AWS identity with `aoss:BatchGetCollection` (to look up the
endpoint), data access to the collection (`aoss:*` on `index/<collection>/agent-evals` and read on the span index;
`deploy/01_collection.py --principals` grants this) and `bedrock:InvokeModel` on the evaluation model. Apply `../pipeline/index-templates/agent-evals.json` first (the script also creates the index
with the same mapping if it is missing).

Run these from your workstation, with `--since` set to about an hour ago in UTC:

```bash
export AOSS_COLLECTION=agentobs-ts AWS_REGION=us-east-1      # <name>-ts from deploy/01_collection.py
# see what the evaluation model would read, no Bedrock calls, no writes
python score_live.py --since 2026-11-18T02:10:00Z --dry-run --dry-run-show 3
# score up to 20 conversations, once (about $0.20)
python score_live.py --since 2026-11-18T02:10:00Z --workers 6 --limit 20
# what is in the index
python check_evals.py 5
```

Each pass scores the oldest unscored conversations first, at most `--limit` per pass, and continues immediately while
a backlog remains. A conversation that cannot be scored (throttling, or a verdict missing any of the three scores or
the hallucination flag) is never stored; it is retried on the next pass and given up, with a log line, after
`--max-retry-passes` (default 5). Run the tests offline with `python -m pytest evaluation/ deploy/`.

Throughput: ~90–120 conversations/min with 6 workers. Cost with Claude Sonnet 5.5 as the evaluation model:
about 1 cent per conversation (≈ 1,000–2,500 input tokens of evidence, ~110 output tokens).

### Continuous scoring

`--loop 600` keeps scoring new conversations every 10 minutes. With the load generator running, that is roughly
$30 a day in evaluation-model calls. Do not leave it running on a laptop: it stops when the laptop sleeps, then
scores the backlog in a burst when it wakes, and it keeps spending until you stop it.

For continuous scoring, run it on a server in your account, for example as a systemd service, a scheduled container
or a scheduled job. Whatever runs it needs the same permissions listed above. The sample's EC2 host role does not
have them by default: it has Bedrock access but no collection access. Stop the loop, or delete the stack with
`deploy/99_cleanup.py`, when you no longer need scores.

## Practical notes

- **Output budget.** Allow the evaluation model 1,000+ output tokens and parse the verdict tolerantly
  (`parse_verdict`): on longer conversations it reasons before it answers. The script never stores a document
  without a score.
- **Model parameters.** Claude 5.5 models do not accept `temperature`; the script does not send it.
- **PPL `parse`** needs a full-string regex (`(?s).*pattern.*`) and capture-group names without underscores.

## Reading the results

`../dashboards/dashboard-3-ai-quality.md` lists the panels and PPL. Scores by use case and model:

```
source = agent-evals
| stats count() as Conversations, avg(score_overall) as o, avg(cost_usd) as cost by use_case, model
| eval `Overall (1-5)` = round(o, 2), `Cost per conversation ($)` = round(cost, 4)
| fields use_case, model, Conversations, `Overall (1-5)`, `Cost per conversation ($)`
| sort use_case, model
```
