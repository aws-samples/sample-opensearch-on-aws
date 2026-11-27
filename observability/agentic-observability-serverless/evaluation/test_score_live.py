"""Offline tests for score_live: verdict acceptance and the backlog/retry watermark (no AWS calls).
Run: python -m pytest evaluation/   or   python evaluation/test_score_live.py"""
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import score_live as s  # noqa: E402


# ---------- verdict acceptance (finding: partial verdicts must not pass)
def test_complete_verdict_accepted():
    sc, halluc = s.accept_verdict({"correct": 5, "relevant": 4, "helpful": 3, "hallucination": False})
    assert sc == {"correct": 5.0, "relevant": 4.0, "helpful": 3.0}
    assert halluc is False


def test_partial_verdict_rejected():
    assert s.accept_verdict({"correct": 5}) is None
    assert s.accept_verdict({"correct": 5, "relevant": 5, "helpful": 5}) is None


def test_invalid_values_rejected():
    base = {"correct": 5, "relevant": 5, "helpful": 5, "hallucination": False}
    assert s.accept_verdict({**base, "correct": 7}) is None
    assert s.accept_verdict({**base, "correct": 0}) is None
    assert s.accept_verdict({**base, "correct": "5"}) is None
    assert s.accept_verdict({**base, "correct": True}) is None
    assert s.accept_verdict({**base, "hallucination": "false"}) is None


# ---------- watermark (finding: backlog and failed conversations must not be skipped)
class FakeStore:
    """In-memory stand-in for the span index and the evaluation index."""

    def __init__(self, n, fail=()):
        self.roots = [{"traceId": f"t{i:05d}", "spanId": f"s{i:05d}", "startTime": f"2026-11-18T00:00:{i:05d}Z"}
                      for i in range(n)]
        self.scored = set()
        self.fail = set(fail)

    def iter_root_pages(self, os_client, since, until, page=500):
        window = [r for r in self.roots if since <= r["startTime"] < until]
        for i in range(0, len(window), page):
            yield window[i:i + page]

    def already_scored(self, os_client, trace_ids):
        return {t for t in trace_ids if t in self.scored}

    def score_one(self, br, item, model_id, run_id):
        if item["traceId"] in self.fail:
            raise RuntimeError("incomplete verdict")
        self.scored.add(item["traceId"])
        return {"traceId": item["traceId"]}


def _patch(store):
    s.iter_root_pages = store.iter_root_pages
    s.already_scored = store.already_scored
    s.score_one = store.score_one
    s.fetch_trace = lambda os_client, trace_id: []
    s.assemble = lambda root, spans: {"traceId": root["traceId"]}
    s.helpers = types.SimpleNamespace(bulk=lambda *a, **k: None)
    s.time.sleep = lambda *_: None
    s.log = lambda *_: None


def _args(limit, max_retry_passes=5):
    return types.SimpleNamespace(limit=limit, max_retry_passes=max_retry_passes, dry_run=False, dry_run_show=0,
                                 judge_model="m", workers=2, failed_passes={})


UNTIL = "2026-11-18T99:99:99Z"
SINCE = "2026-11-18T00:00:00000Z"


def test_backlog_beyond_limit_is_drained():
    store = FakeStore(5001)
    _patch(store)
    args, give_up = _args(limit=5000), set()
    n, nxt, backlog = s.run_once(None, None, args, "run", SINCE, UNTIL, give_up)
    assert (n, backlog) == (5000, True)
    assert nxt <= store.roots[5000]["startTime"]  # watermark has not passed the unscored conversation
    n, nxt, backlog = s.run_once(None, None, args, "run", nxt, UNTIL, give_up)
    assert (n, backlog) == (1, False)
    assert len(store.scored) == 5001 and nxt == UNTIL


def test_failed_conversation_is_retried_then_given_up():
    store = FakeStore(10, fail={"t00003"})
    _patch(store)
    args, give_up = _args(limit=100, max_retry_passes=2), set()
    n, nxt, _ = s.run_once(None, None, args, "run", SINCE, UNTIL, give_up)
    assert n == 9 and nxt == store.roots[3]["startTime"]  # watermark held at the failed conversation
    store.fail.clear()  # transient failure recovers
    n, nxt, _ = s.run_once(None, None, args, "run", nxt, UNTIL, give_up)
    assert n == 1 and "t00003" in store.scored and nxt == UNTIL


def test_permanent_failure_does_not_pin_the_watermark():
    store = FakeStore(5, fail={"t00001"})
    _patch(store)
    args, give_up = _args(limit=100, max_retry_passes=2), set()
    _, nxt, _ = s.run_once(None, None, args, "run", SINCE, UNTIL, give_up)
    assert nxt == store.roots[1]["startTime"]
    _, nxt, _ = s.run_once(None, None, args, "run", nxt, UNTIL, give_up)
    assert "t00001" in give_up and nxt == UNTIL


# ---------- finding: duplicate roots within one page must be scored once
def test_duplicate_root_in_one_page_scored_once():
    page = [{"traceId": "t1", "spanId": "a", "startTime": "2026-11-18T00:00:00001Z"},
            {"traceId": "t1", "spanId": "b", "startTime": "2026-11-18T00:00:00002Z"},  # same traceId, same page
            {"traceId": "t2", "spanId": "c", "startTime": "2026-11-18T00:00:00003Z"}]
    s.iter_root_pages = lambda *a, **k: iter([page])
    s.already_scored = lambda *a, **k: set()
    todo, total, exhausted = s.next_unscored(None, SINCE, UNTIL, 100)
    assert [r["traceId"] for r in todo] == ["t1", "t2"] and total == 2 and exhausted is True


# ---------- finding: one-shot --limit is a total, not a per-pass batch that drains the backlog
def test_one_shot_runs_one_pass_even_with_backlog():
    calls = []

    def do_pass():
        calls.append(1)
        return 20, True  # scored 20, backlog remains

    total = s.drive(do_pass, loop=0, dry_run=False, sleep=lambda *_: None)
    assert total == 20 and len(calls) == 1  # honours the total limit; does not keep draining


def test_continuous_drains_backlog_then_waits():
    seq = [(20, True), (20, True), (5, False)]
    calls, sleeps = [], []

    def do_pass():
        calls.append(1)
        return seq[len(calls) - 1]

    def fake_sleep(x):
        sleeps.append(x)
        raise SystemExit  # stop the otherwise-infinite continuous loop once caught up

    try:
        s.drive(do_pass, loop=600, dry_run=False, sleep=fake_sleep)
    except SystemExit:
        pass
    assert len(calls) == 3 and sleeps == [600]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
