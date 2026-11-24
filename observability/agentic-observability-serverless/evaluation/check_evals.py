"""Read-only: what is in agent-evals? Counts, averages by model and use case, label split, and the latest N documents."""
import os
import sys
import boto3
from opensearchpy import OpenSearch, RequestsAWSV4SignerAuth, RequestsHttpConnection

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
s = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"))
d = s.client("opensearchserverless", region_name=os.environ.get("AWS_REGION", "us-east-1")).batch_get_collection(names=[os.environ.get("AOSS_COLLECTION", "agentobs-ts")])["collectionDetails"][0]
c = OpenSearch(hosts=[{"host": d["collectionEndpoint"].replace("https://", ""), "port": 443}],
               http_auth=RequestsAWSV4SignerAuth(s.get_credentials(), os.environ.get("AWS_REGION", "us-east-1"), "aoss"), use_ssl=True, verify_certs=True,
               connection_class=RequestsHttpConnection, timeout=60)
I = "agent-evals"
r = c.search(index=I, body={"size": 0, "aggs": {
    "by_model": {"terms": {"field": "model"}, "aggs": {
        "overall": {"avg": {"field": "score_overall"}}, "correct": {"avg": {"field": "score_correct"}},
        "helpful": {"avg": {"field": "score_helpful"}}, "cost": {"avg": {"field": "cost_usd"}},
        "halluc": {"filter": {"term": {"hallucination": True}}}, "fail": {"filter": {"term": {"label": "fail"}}},
        "latency": {"avg": {"field": "duration_ms"}}}},
    "by_use_case": {"terms": {"field": "use_case"}, "aggs": {"by_model": {"terms": {"field": "model"}, "aggs": {"overall": {"avg": {"field": "score_overall"}}}}}},
    "judge_cost": {"sum": {"field": "judge_cost_usd"}}, "distinct": {"cardinality": {"field": "traceId"}}}})
print(f"documents: {r['hits']['total']['value']}  distinct traces: {r['aggregations']['distinct']['value']}  judge spend so far: ${r['aggregations']['judge_cost']['value']:.2f}")
print("\nby model:  n  overall  correct  helpful  halluc  fail  avg cost/conv  avg latency")
for b in r["aggregations"]["by_model"]["buckets"]:
    a = b
    print(f"  {b['key']:18s} {b['doc_count']:5d}  {a['overall']['value'] or 0:.2f}     {a['correct']['value'] or 0:.2f}     {a['helpful']['value'] or 0:.2f}     "
          f"{a['halluc']['doc_count']:4d}  {a['fail']['doc_count']:4d}   ${a['cost']['value'] or 0:.4f}      {(a['latency']['value'] or 0) / 1000:.1f}s")
print("\nby use case / model: overall")
for u in r["aggregations"]["by_use_case"]["buckets"]:
    parts = ", ".join(f"{m['key']}={m['overall']['value'] or 0:.2f} (n={m['doc_count']})" for m in u["by_model"]["buckets"])
    print(f"  {u['key']:18s} {parts}")
print(f"\nlatest {N}:")
for h in c.search(index=I, body={"size": N, "sort": [{"scored_at": "desc"}]})["hits"]["hits"]:
    d = h["_source"]
    print(f"  {d['@timestamp'][:19]} {d['model']:16s} {d['use_case']:16s} overall={d['score_overall']} c={d['score_correct']} r={d['score_relevant']} h={d['score_helpful']} "
          f"halluc={d['hallucination']} {d['label']}  ${d['cost_usd'] or 0:.4f}")
    print(f"      Q: {d['question'][:110]}")
    print(f"      why: {d['explanation'][:200]}")
