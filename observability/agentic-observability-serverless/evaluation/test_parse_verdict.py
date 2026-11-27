"""Tests for score_live.parse_verdict (no network). Run: python -m pytest evaluation/  or  python evaluation/test_parse_verdict.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import score_live as s  # noqa: E402


def test_json_with_prose_and_unescaped_quote():
    v = s.parse_verdict('Analysis first... {"correct": 4, "relevant": 5, "helpful": 3, "hallucination": false, "reason": "The "Comet Book" price matches."}')
    assert (v["correct"], v["relevant"], v["helpful"]) == (4, 5, 3)
    assert v["hallucination"] is False
    assert "Comet Book" in v["reason"]


def test_truncated_json_still_yields_scores():
    v = s.parse_verdict('{"correct": 5, "relevant": 5, "helpful": 5, "hallucination": false, "reason')
    assert (v["correct"], v["relevant"], v["helpful"]) == (5, 5, 5)
    assert v["hallucination"] is False


def test_fenced_json():
    v = s.parse_verdict('```json\n{"correct": 2, "relevant": 4, "helpful": 1, "hallucination": true, "reason": "Invented a discount."}\n```')
    assert v["correct"] == 2 and v["hallucination"] is True and v["reason"] == "Invented a discount."


def test_no_verdict_returns_empty():
    assert s.parse_verdict("no json here") == {}


def test_ambiguous_two_objects_rejected():
    # a passing example object followed by the real (failing) verdict: reject, do not take the first
    txt = ('For instance a good answer would be {"correct": 5, "relevant": 5, "helpful": 5, "hallucination": false, "reason": "ok"}. '
           'Here the price was invented, so: {"correct": 2, "relevant": 3, "helpful": 1, "hallucination": true, "reason": "invented price"}')
    assert s.parse_verdict(txt) == {}


def test_ambiguous_conflicting_regex_values_rejected():
    # neither object parses (unescaped quotes), but two different "correct" values -> ambiguous
    txt = '{"correct": 5, "helpful": 5, "reason": "the "A" book"} then {"correct": 1, "helpful": 2, "reason": "the "B" book"}'
    assert s.parse_verdict(txt) == {}


def test_single_verdict_with_nonverdict_json_is_fine():
    # a non-verdict JSON object alongside the verdict must not count as ambiguity
    txt = 'context {"note": "n/a"} verdict {"correct": 4, "relevant": 4, "helpful": 4, "hallucination": false, "reason": "fine"}'
    v = s.parse_verdict(txt)
    assert (v["correct"], v["hallucination"]) == (4, False)


def test_clamp():
    assert s.clamp(7) == 5.0 and s.clamp(0) == 1.0 and s.clamp("3") == 3.0 and s.clamp("x") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
