import json

import pytest

from src.llm.llm_client import extract_json
from src.llm.prompts import PROMPT_VERSION, build_user_message
from src.schemas import ClassifiedPR, UpdateAssessment, Verdict, VerdictMeta
from src.storage.verdict_cache import VerdictCache
from tests.test_deterministic import make_record


def make_classified(pr=16, dep="Redis", to="2.13.17", latest="3.1.13",
                    grouped=False) -> ClassifiedPR:
    rec = make_record(pr, dep, to)
    rec = rec.model_copy(update={"is_grouped": grouped})
    return ClassifiedPR(
        record=rec, status="STALE_CANDIDATE",
        assessments=[UpdateAssessment(dependency=dep, from_ver="1.0", to_ver=to,
                                      latest_ver=latest, is_behind=True)],
    )


# ── model routing (no API needed: pure logic on config values) ────────────

def test_major_gap_escalates(monkeypatch):
    from src.config import Config
    from src.llm.llm_client import LLMClient
    cfg = Config(github_token="x", repos=["o/r"], anthropic_api_key="k",
                 model_default="haiku", model_escalation="sonnet")
    llm = LLMClient.__new__(LLMClient)  # skip __init__: no client needed for routing
    llm._config = cfg
    assert llm.choose_model(make_classified(to="2.13.17", latest="3.1.13")) == "sonnet"
    assert llm.choose_model(make_classified(to="3.1.0", latest="3.1.13")) == "haiku"
    assert llm.choose_model(make_classified(to="3.1.0", latest="3.1.13",
                                            grouped=True)) == "sonnet"


# ── verdict cache ─────────────────────────────────────────────────────────

def test_cache_round_trip(tmp_path):
    path = tmp_path / "cache.json"
    cache = VerdictCache(str(path))
    cp = make_classified()
    key = VerdictCache.key_for(cp)
    assert cache.get(key) is None

    verdict = Verdict(verdict="SUPERSEDED", recommended_action="recreate to 3.1.13",
                      risk_of_newer_version="low", reasoning="ok")
    meta = VerdictMeta(model="haiku", prompt_version=PROMPT_VERSION,
                       input_tokens=100, output_tokens=50)
    cache.put(key, verdict, meta)

    # fresh instance reads from disk; hit costs zero tokens
    got_verdict, got_meta = VerdictCache(str(path)).get(key)
    assert got_verdict == verdict
    assert got_meta.cache_hit is True
    assert got_meta.input_tokens == 0


def test_cache_key_is_repo_independent_but_version_specific():
    a = make_classified(pr=1, to="2.13.17", latest="3.1.13")
    b = make_classified(pr=99, to="2.13.17", latest="3.1.13")
    b.record = b.record.model_copy(update={"repo": "other/repo"})
    assert VerdictCache.key_for(a) == VerdictCache.key_for(b)  # cross-repo reuse
    c = make_classified(to="2.13.17", latest="3.2.0")
    assert VerdictCache.key_for(a) != VerdictCache.key_for(c)  # new latest = new question
    assert PROMPT_VERSION in VerdictCache.key_for(a)


# ── response parsing ──────────────────────────────────────────────────────

def test_extract_json_from_fenced_output():
    text = 'Here is my verdict:\n```json\n{"verdict": "STILL_VALID"}\n```'
    assert json.loads(extract_json(text))["verdict"] == "STILL_VALID"


def test_extract_json_absent():
    assert extract_json("no json here") is None


def test_verdict_rejects_invalid_enum():
    with pytest.raises(Exception):
        Verdict.model_validate({"verdict": "MERGE_IT", "recommended_action": "x",
                                "risk_of_newer_version": "low", "reasoning": "y"})


# ── prompt content ────────────────────────────────────────────────────────

def test_user_message_carries_the_facts():
    cp = make_classified()
    cp.record = cp.record.model_copy(update={"release_notes_excerpt": "## 3.1.13 notes"})
    msg = build_user_message(cp)
    assert "2.13.17" in msg and "3.1.13" in msg
    assert "BEHIND" in msg
    assert "## 3.1.13 notes" in msg
