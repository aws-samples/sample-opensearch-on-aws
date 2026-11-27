"""Online evaluation of agent conversations stored in an OpenSearch Serverless time series collection.

Pattern follows the OpenSearch Observability Stack "Evaluation & Scoring" docs: list root agent spans -> skip the ones
already scored -> fetch the trace -> judge -> write one score document per conversation, linked by traceId.
Writes directly to the collection (index agent-evals) as flat documents, not as OTel gen_ai.evaluation.result events.

Usage (AWS profile with collection data access + bedrock:InvokeModel; env AOSS_COLLECTION, AWS_REGION):
  python evaluation/score_live.py --since 2026-11-18T02:10:00Z --dry-run --limit 5     # assemble evidence, no Bedrock, no writes
  python evaluation/score_live.py --since 2026-11-18T02:10:00Z                         # score everything since, once
  python evaluation/score_live.py --since 2026-11-18T02:10:00Z --loop 600              # then keep scoring new ones every 10 min
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import random
import re
import sys
import time
import uuid

import boto3
from botocore.config import Config
from opensearchpy import OpenSearch, RequestsAWSV4SignerAuth, RequestsHttpConnection, helpers

import os
REGION = os.environ.get("AWS_REGION", "us-east-1")
COLLECTION = os.environ.get("AOSS_COLLECTION", "agentobs-ts")  # <name>-ts as created by deploy/01_collection.py
SPAN_INDEX = "otel-v1-apm-span-r4-*"
EVAL_INDEX = "agent-evals"
ROOT_NAME = "astronomy_shop_agent_workflow.workflow"
# Bedrock inference profile prefix (us, eu, apac, global): set MODEL_PREFIX outside the US, or pass --judge-model
MODEL_PREFIX = os.environ.get("MODEL_PREFIX", "us")
JUDGE_MODEL = f"{MODEL_PREFIX}.anthropic.claude-sonnet-5-5"
EVAL_VERSION = "v1"
# USD per 1M tokens (AWS Price List, us-east-1, on-demand Standard, 17 Nov 2026)
PRICES = {"haiku": (0.11, 0.55), "sonnet": (2.20, 11.00)}
MAX_TOOL_CHARS = 3500      # per tool result given to the judge
MAX_EVIDENCE_CHARS = 9000  # all tool results together

EVAL_MAPPING = {
    "settings": {"index.timestamp_field": "@timestamp"},
    "mappings": {"properties": {
        "@timestamp": {"type": "date"},
        "traceId": {"type": "keyword"}, "spanId": {"type": "keyword"},
        "question": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}},
        "answer": {"type": "text"},
        "use_case": {"type": "keyword"}, "user_id": {"type": "keyword"},
        "model": {"type": "keyword"},
        "input_tokens": {"type": "long"}, "output_tokens": {"type": "long"}, "cost_usd": {"type": "double"},
        "duration_ms": {"type": "double"}, "llm_calls": {"type": "integer"},
        "tool_calls": {"type": "integer"}, "tool_names": {"type": "keyword"},
        "answered": {"type": "boolean"}, "error": {"type": "boolean"},
        "score_correct": {"type": "float"}, "score_relevant": {"type": "float"}, "score_helpful": {"type": "float"},
        "score_overall": {"type": "float"}, "hallucination": {"type": "boolean"},
        "label": {"type": "keyword"}, "explanation": {"type": "text"},
        "judge_model": {"type": "keyword"}, "judge_input_tokens": {"type": "long"}, "judge_output_tokens": {"type": "long"},
        "judge_cost_usd": {"type": "double"},
        "eval_type": {"type": "keyword"}, "eval_run_id": {"type": "keyword"}, "eval_version": {"type": "keyword"},
        "scored_at": {"type": "date"},
    }},
}

RUBRIC = """You are a strict quality judge for an online shop's AI assistant (Astronomy Shop, sells telescopes and astronomy gear).
You will be given the shopper's question, the assistant's final answer, and the RAW RESULTS the assistant's tools returned
(product catalogue entries, shipping quotes, currencies, cart/order responses). Judge the answer ONLY against those tool results.

Score each 1-5 (5 = excellent):
- correct: every fact in the answer (product names, prices, availability, shipping cost, currency, order details) is supported
  by the tool results. Invented or wrong facts -> 1 or 2. If the assistant honestly says it cannot do something, do not
  penalise correctness.
- relevant: the answer addresses what the shopper actually asked.
- helpful: the shopper can act on it (clear recommendation, price, next step). Vague or evasive -> low.
Also set hallucination=true if the answer states any fact not present in the tool results (or contradicts them).
If no tool was called and the answer states shop facts anyway, that is a hallucination.

Return ONLY a JSON object, no prose, exactly like:
{"correct": 5, "relevant": 5, "helpful": 4, "hallucination": false, "reason": "one sentence, max 40 words"}"""


def log(msg):
    print(f"{dt.datetime.now(dt.timezone.utc):%H:%M:%S}Z {msg}", flush=True)


def clients(profile):
    s = boto3.Session(profile_name=profile)
    ep = s.client("opensearchserverless", region_name=REGION).batch_get_collection(names=[COLLECTION])["collectionDetails"][0]["collectionEndpoint"]
    os_client = OpenSearch(hosts=[{"host": ep.replace("https://", ""), "port": 443}],
                           http_auth=RequestsAWSV4SignerAuth(s.get_credentials(), REGION, "aoss"), use_ssl=True, verify_certs=True,
                           connection_class=RequestsHttpConnection, timeout=60, max_retries=3, retry_on_timeout=True)
    br = s.client("bedrock-runtime", region_name=REGION, config=Config(retries={"max_attempts": 8, "mode": "adaptive"}, read_timeout=120))
    return os_client, br


def ensure_index(os_client):
    if os_client.indices.exists(index=EVAL_INDEX):
        return False
    os_client.indices.create(index=EVAL_INDEX, body=EVAL_MAPPING)
    return True


def nested_get(doc, dotted):
    """attributes are an object whose keys contain dots: attributes -> 'demo.agent.use_case'."""
    if dotted in doc:
        return doc[dotted]
    if "." in dotted:
        head, tail = dotted.split(".", 1)
        inner = doc.get(head)
        if isinstance(inner, dict):
            if tail in inner:
                return inner[tail]
            return nested_get(inner, tail)
    return None


def as_json(v):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def text_parts(messages, role=None):
    """Semconv messages [{role, parts:[{type:'text', content}]}] -> joined text for the given role (last message wins)."""
    out = ""
    for m in messages if isinstance(messages, list) else []:
        if not isinstance(m, dict) or (role and m.get("role") != role):
            continue
        txt = " ".join(p.get("content", "") for p in m.get("parts", []) if isinstance(p, dict) and p.get("type") == "text" and p.get("content"))
        if txt:
            out = txt
    return out


def tool_result_text(v):
    """execute_tool gen_ai.tool.call.result = {"output": {"kwargs": {"content": "..."}}} (LangChain ToolMessage)."""
    j = as_json(v)
    if isinstance(j, dict):
        out = j.get("output")
        if isinstance(out, dict):
            kw = out.get("kwargs", {})
            if isinstance(kw, dict) and "content" in kw:
                c = kw["content"]
                return c if isinstance(c, str) else json.dumps(c)
        return json.dumps(j)
    return str(j)


def price(model, tin, tout):
    m = (model or "").lower()
    for k, (pi, po) in PRICES.items():
        if k in m:
            return (tin or 0) / 1e6 * pi + (tout or 0) / 1e6 * po
    return None


def iter_root_pages(os_client, since, until, page=500):
    """Yield pages of conversation root spans in [since, until), oldest first, until the window is exhausted."""
    search_after = None
    while True:
        body = {"size": page,
                "query": {"bool": {"filter": [{"term": {"serviceName": "agent"}}, {"term": {"name": ROOT_NAME}},
                                              {"range": {"startTime": {"gte": since, "lt": until}}}]}},
                "sort": [{"startTime": "asc"}, {"spanId": "asc"}],
                "_source": ["traceId", "spanId", "startTime", "durationInNanos", "status.code",
                            "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"]}
        if search_after:
            body["search_after"] = search_after
        hits = os_client.search(index=SPAN_INDEX, body=body)["hits"]["hits"]
        if not hits:
            return
        yield [h["_source"] for h in hits]
        search_after = hits[-1]["sort"]
        if len(hits) < page:
            return


def next_unscored(os_client, since, until, limit, skip=frozenset()):
    """Oldest unscored conversations in the window, at most `limit`. Returns (todo, total_seen, window_exhausted).
    Already-scored traces and traces in `skip` (given up) do not count against the limit."""
    todo, seen, total = [], set(), 0
    for page in iter_root_pages(os_client, since, until):
        unique = []
        for r in page:  # one root per traceId, deduping within a page as well as across pages
            if r["traceId"] not in seen:
                seen.add(r["traceId"])
                unique.append(r)
        page = unique
        total += len(page)
        done = already_scored(os_client, [r["traceId"] for r in page])
        for r in page:
            if r["traceId"] in done or r["traceId"] in skip:
                continue
            if len(todo) >= limit:
                return todo, total, False
            todo.append(r)
    return todo, total, True


def already_scored(os_client, trace_ids):
    if not os_client.indices.exists(index=EVAL_INDEX):
        return set()
    done = set()
    ids = list(trace_ids)
    for i in range(0, len(ids), 1000):
        r = os_client.search(index=EVAL_INDEX, body={"size": 0, "query": {"terms": {"traceId": ids[i:i + 1000]}},
                                                     "aggs": {"t": {"terms": {"field": "traceId", "size": 1000}}}})
        done.update(b["key"] for b in r["aggregations"]["t"]["buckets"])
    return done


def fetch_trace(os_client, trace_id):
    r = os_client.search(index=SPAN_INDEX, body={"size": 500, "query": {"term": {"traceId": trace_id}}, "sort": [{"startTime": "asc"}],
                                                 "_source": ["spanId", "name", "serviceName", "startTime", "durationInNanos", "status.code",
                                                             "gen_ai.operation.name", "gen_ai.tool.name", "gen_ai.response.model", "gen_ai.request.model",
                                                             "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.input.messages",
                                                             "gen_ai.output.messages", "gen_ai.tool.call.result", "attributes.demo.agent.use_case",
                                                             "attributes.user.id", "attributes.http.response.status_code", "attributes.http.status_code"]})
    return [h["_source"] for h in r["hits"]["hits"]]


def assemble(root, spans):
    """Turn one conversation's spans into the evidence the judge reads + the metrics we store."""
    seen_span_ids, uniq = set(), []
    for sp in spans:  # defensive: keep one document per spanId
        if sp.get("spanId") not in seen_span_ids:
            seen_span_ids.add(sp.get("spanId"))
            uniq.append(sp)
    spans = uniq
    ask = next((s for s in spans if s.get("name") == "user_ask_agent"), None)
    chats = [s for s in spans if s.get("name") == "ChatLLM.chat"]
    tools = [s for s in spans if nested_get(s, "gen_ai.operation.name") == "execute_tool"]
    rootspan = next((s for s in spans if s.get("name") == ROOT_NAME), root)

    question = ""
    if ask:
        question = text_parts(as_json(nested_get(ask, "gen_ai.input.messages")), "user")
    if not question:
        j = as_json(nested_get(rootspan, "gen_ai.input.messages"))
        if isinstance(j, dict) and isinstance(j.get("args"), list) and len(j["args"]) > 1:
            question = str(j["args"][1])
    if not question and chats:
        question = text_parts(as_json(nested_get(chats[0], "gen_ai.input.messages")), "user")

    answer = ""
    for c in reversed(chats):
        answer = text_parts(as_json(nested_get(c, "gen_ai.output.messages")), "assistant")
        if answer:
            break

    model = next((nested_get(c, "gen_ai.response.model") or nested_get(c, "gen_ai.request.model") for c in chats if nested_get(c, "gen_ai.response.model") or nested_get(c, "gen_ai.request.model")), None)
    tin = sum(int(nested_get(c, "gen_ai.usage.input_tokens") or 0) for c in chats)
    tout = sum(int(nested_get(c, "gen_ai.usage.output_tokens") or 0) for c in chats)
    if tin == 0:
        tin = int(nested_get(rootspan, "gen_ai.usage.input_tokens") or 0)
        tout = int(nested_get(rootspan, "gen_ai.usage.output_tokens") or 0)

    evidence, total = [], 0
    for t in tools:
        name = nested_get(t, "gen_ai.tool.name") or t.get("name", "").replace("execute_tool ", "")
        res = tool_result_text(nested_get(t, "gen_ai.tool.call.result"))
        if len(res) > MAX_TOOL_CHARS:
            res = res[:MAX_TOOL_CHARS] + f" ...[truncated, {len(res)} chars]"
        if total + len(res) > MAX_EVIDENCE_CHARS:
            res = res[:max(0, MAX_EVIDENCE_CHARS - total)] + " ...[truncated]"
        total += len(res)
        evidence.append(f"### tool: {name}\n{res}")
        if total >= MAX_EVIDENCE_CHARS:
            break

    http_err = any(str(nested_get(s, "attributes.http.response.status_code") or nested_get(s, "attributes.http.status_code") or "").startswith("5")
                   for s in spans if s.get("name") in ("POST /prompt", "POST"))
    error = bool(http_err or (nested_get(rootspan, "status.code") == 2))

    return {
        "@timestamp": rootspan.get("startTime") or root.get("startTime"),
        "traceId": root["traceId"], "spanId": rootspan.get("spanId") or root.get("spanId"),
        "question": question[:1000], "answer": answer[:4000],
        "use_case": (nested_get(ask, "attributes.demo.agent.use_case") if ask else None) or "unknown",
        "user_id": (nested_get(ask, "attributes.user.id") if ask else None),
        "model": model or "unknown",
        "input_tokens": tin, "output_tokens": tout, "cost_usd": price(model, tin, tout),
        "duration_ms": (rootspan.get("durationInNanos") or root.get("durationInNanos") or 0) / 1e6,
        "llm_calls": len(chats), "tool_calls": len(tools),
        "tool_names": sorted({nested_get(t, "gen_ai.tool.name") or t.get("name", "").replace("execute_tool ", "") for t in tools}),
        "answered": bool(answer.strip()), "error": error,
        "_evidence": "\n\n".join(evidence) if evidence else "(no tool was called)",
    }


def judge(br, item, model_id):
    user = (f"SHOPPER QUESTION:\n{item['question'] or '(missing)'}\n\n"
            f"ASSISTANT FINAL ANSWER:\n{item['answer'] or '(no answer / error)'}\n\n"
            f"TOOL RESULTS THE ASSISTANT SAW:\n{item['_evidence']}")
    # Claude 5.5 models reject     emperature. 1200 output tokens: the evaluation model often reasons before the JSON.
    r = br.converse(modelId=model_id, system=[{"text": RUBRIC}], messages=[{"role": "user", "content": [{"text": user}]}],
                    inferenceConfig={"maxTokens": 1200})
    txt = "".join(c.get("text", "") for c in r["output"]["message"]["content"])
    usage = r.get("usage", {})
    verdict = parse_verdict(txt)
    if r.get("stopReason") == "max_tokens" and not verdict:
        raise RuntimeError("judge hit max_tokens without a verdict")
    return verdict, usage.get("inputTokens", 0), usage.get("outputTokens", 0), txt


VERDICT_KEYS = ("correct", "relevant", "helpful", "hallucination")


def json_objects(txt):
    """Every balanced, parseable top-level {...} object in txt, in order (quotes and escapes respected)."""
    objs, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(txt):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    objs.append(json.loads(txt[start:i + 1]))
                except Exception:
                    pass
                start = None
    return objs


def parse_verdict(txt):
    """Return the single verdict object. Ambiguous output -> {} so the caller rejects and retries:
    the judge sometimes shows a passing example object before its real (failing) verdict, and taking the first
    regex match would score the wrong one. If exactly one JSON verdict parses, use it; if several do, reject.
    Otherwise (unescaped quote in `reason`, truncation) fall back to regex, but only when the fields are unambiguous."""
    verdicts = [o for o in json_objects(txt) if isinstance(o, dict) and any(k in o for k in VERDICT_KEYS)]
    if len(verdicts) == 1:
        return verdicts[0]
    if len(verdicts) > 1:
        return {}  # more than one candidate verdict: ambiguous
    # no complete JSON object parsed; reject if a score key appears more than once with differing values
    for k in ("correct", "relevant", "helpful"):
        if len({m.group(1) for m in re.finditer(rf'"{k}"\s*:\s*([0-9.]+)', txt)}) > 1:
            return {}
    v = {}
    for k in ("correct", "relevant", "helpful"):
        m = re.search(rf'"{k}"\s*:\s*([0-9.]+)', txt)
        if m:
            v[k] = float(m.group(1))
    m = re.search(r'"hallucination"\s*:\s*(true|false)', txt, re.I)
    if m:
        v["hallucination"] = m.group(1).lower() == "true"
    m = re.search(r'"reason"\s*:\s*"(.*?)"\s*}?\s*$', txt, re.S) or re.search(r'"reason"\s*:\s*"([^"]*)', txt, re.S)
    if m:
        v["reason"] = m.group(1).strip()
    return v if any(k in v for k in ("correct", "relevant", "helpful")) else {}


def clamp(v):
    try:
        return max(1.0, min(5.0, float(v)))
    except Exception:
        return None


def accept_verdict(verdict):
    """All three scores present, numeric and within 1..5, and a boolean hallucination flag; else None (caller retries)."""
    scores = {}
    for k in ("correct", "relevant", "helpful"):
        v = verdict.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= float(v) <= 5:
            return None
        scores[k] = float(v)
    if not isinstance(verdict.get("hallucination"), bool):
        return None
    return scores, verdict["hallucination"]


def score_one(br, item, model_id, run_id):
    verdict, jin, jout, raw = judge(br, item, model_id)
    accepted = accept_verdict(verdict)
    if accepted is None:  # incomplete or malformed verdict: never store it, let the caller retry
        raise RuntimeError(f"incomplete verdict: {raw[:120]!r}")
    sc, halluc = accepted
    overall = round(sum(sc.values()) / 3, 2)
    doc = {k: v for k, v in item.items() if not k.startswith("_")}
    doc.update({
        "score_correct": sc["correct"], "score_relevant": sc["relevant"], "score_helpful": sc["helpful"], "score_overall": overall,
        "hallucination": halluc,
        "label": ("fail" if (overall < 3.5 or halluc or item["error"] or not item["answered"]) else "pass"),
        "explanation": (verdict.get("reason") or raw[:500])[:500],
        "judge_model": model_id, "judge_input_tokens": jin, "judge_output_tokens": jout, "judge_cost_usd": price(model_id, jin, jout),
        "eval_type": "online", "eval_run_id": run_id, "eval_version": EVAL_VERSION,
        "scored_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    return doc


def run_once(os_client, br, args, run_id, since, until, give_up):
    """One pass. Returns (scored, next_since, backlog_left). next_since never moves past an unscored conversation:
    it is the oldest failed conversation still being retried, else the last conversation of a partial batch, else until."""
    todo, total, exhausted = next_unscored(os_client, since, until, args.limit, give_up)
    log(f"window {since} -> {until[:19]}Z: {total} conversations seen, {len(todo)} to score"
        + ("" if exhausted else f" (batch limit {args.limit}; more remain)"))
    if not todo:
        return 0, until, False  # everything in the window is scored or given up

    if args.dry_run:
        for r in todo[: min(len(todo), args.dry_run_show)]:
            item = assemble(r, fetch_trace(os_client, r["traceId"]))
            print("\n" + "=" * 100)
            print(f"trace {item['traceId']}  {item['@timestamp']}  model={item['model']}  use_case={item['use_case']}  "
                  f"tokens={item['input_tokens']}/{item['output_tokens']}  cost=${item['cost_usd'] or 0:.4f}  tools={item['tool_names']}  "
                  f"answered={item['answered']} error={item['error']}")
            print(f"Q: {item['question']}")
            print(f"A: {item['answer'][:600]}")
            print(f"EVIDENCE ({len(item['_evidence'])} chars): {item['_evidence'][:500]} ...")
        sizes = []
        for r in todo[:30]:
            it = assemble(r, fetch_trace(os_client, r["traceId"]))
            sizes.append(len(it["_evidence"]) + len(it["question"]) + len(it["answer"]) + len(RUBRIC))
        avg_tokens = (sum(sizes) / max(1, len(sizes))) / 4
        est = len(todo) * (avg_tokens / 1e6 * PRICES["sonnet"][0] + 120 / 1e6 * PRICES["sonnet"][1])
        log(f"DRY RUN: no Bedrock calls, no writes. ~{avg_tokens:.0f} judge input tokens/conversation -> est. ${est:.2f} for {len(todo)} conversations with Sonnet")
        return 0, since, False

    docs, failed = [], []
    t0 = time.time()

    def work(r):
        for attempt in range(4):
            try:
                item = assemble(r, fetch_trace(os_client, r["traceId"]))
                return score_one(br, item, args.judge_model, run_id)
            except Exception as e:  # throttling, transient network, incomplete verdict
                if attempt == 3:
                    log(f"FAILED {r['traceId']}: {type(e).__name__}: {str(e)[:160]}")
                    return None
                time.sleep(2 ** attempt + random.random())

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (r, doc) in enumerate(zip(todo, ex.map(work, todo)), 1):
            if doc is None:
                failed.append(r)
            else:
                docs.append(doc)
            if len(docs) >= 50:
                helpers.bulk(os_client, ({"_index": EVAL_INDEX, "_source": d} for d in docs), request_timeout=60)
                docs = []
            if i % 25 == 0 or i == len(todo):
                rate = i / max(1, time.time() - t0)
                log(f"  {i}/{len(todo)} scored  ({rate * 60:.0f}/min, {len(failed)} failed)")
    if docs:
        helpers.bulk(os_client, ({"_index": EVAL_INDEX, "_source": d} for d in docs), request_timeout=60)

    # retry bookkeeping: a conversation that fails on --max-retry-passes consecutive passes is given up and logged
    for r in failed:
        args.failed_passes[r["traceId"]] = args.failed_passes.get(r["traceId"], 0) + 1
        if args.failed_passes[r["traceId"]] >= args.max_retry_passes:
            give_up.add(r["traceId"])
            log(f"GIVING UP on {r['traceId']} (started {r['startTime']}) after {args.max_retry_passes} passes")
    pending = [r["startTime"] for r in failed if r["traceId"] not in give_up]
    if not exhausted:
        pending.append(todo[-1]["startTime"])
    next_since = min(pending) if pending else until
    return len(todo) - len(failed), next_since, not exhausted


def drive(do_pass, loop, dry_run, sleep=time.sleep):
    """Run evaluation passes. One-shot (loop falsy): exactly one pass, so --limit caps the total scored and any larger
    backlog is left for the next invocation. Continuous (loop > 0): keep going while a backlog remains, otherwise wait
    `loop` seconds for new conversations. do_pass() -> (scored, backlog_remains). Returns the total scored."""
    total = 0
    while True:
        scored, backlog = do_pass()
        total += scored
        if dry_run or not loop:
            return total
        if not backlog:
            sleep(loop)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # answers can contain any script
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=None, help="AWS profile; default uses the environment credential chain")
    ap.add_argument("--since", required=True, help="ISO time, e.g. 2026-11-18T02:10:00Z")
    ap.add_argument("--until", default=None, help="ISO time; default = now minus --settle seconds")
    ap.add_argument("--settle", type=int, default=120, help="only score conversations older than this many seconds, so all child spans have landed")
    ap.add_argument("--limit", type=int, default=5000, help="one-shot run: total conversations to score, then stop (a larger backlog is left for the next run). With --loop: the per-pass batch size")
    ap.add_argument("--max-retry-passes", type=int, default=5, help="give up on a conversation after it fails on this many passes")
    ap.add_argument("--judge-model", default=JUDGE_MODEL)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="assemble evidence only: no Bedrock, no index creation, no writes")
    ap.add_argument("--dry-run-show", type=int, default=3)
    ap.add_argument("--loop", type=int, default=0, help="seconds between passes; 0 = run once")
    ap.add_argument("--reset-index", action="store_true", help="DELETE the agent-evals index first and start clean")
    args = ap.parse_args()
    args.failed_passes = {}

    os_client, br = clients(args.profile)
    run_id = f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    if args.reset_index and not args.dry_run and os_client.indices.exists(index=EVAL_INDEX):
        os_client.indices.delete(index=EVAL_INDEX)
        log(f"index {EVAL_INDEX}: deleted (reset)")
        time.sleep(3)
    if not args.dry_run:
        log(f"index {EVAL_INDEX}: {'created' if ensure_index(os_client) else 'exists'}")
    log(f"run {run_id}  judge={args.judge_model}  workers={args.workers}")
    state, give_up = {"since": args.since}, set()

    def do_pass():
        until = args.until or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=args.settle)).isoformat()
        n, state["since"], backlog = run_once(os_client, br, args, run_id, state["since"], until, give_up)
        log(f"pass done: {n} new score documents; next window starts {state['since']}")
        return n, backlog

    drive(do_pass, args.loop, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
