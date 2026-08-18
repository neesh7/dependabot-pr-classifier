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
    cfg = Config(github_token="x", repos=["o/r"],
                 model_default="mini", model_escalation="full")
    llm = LLMClient.__new__(LLMClient)  # skip __init__: no client needed for routing
    llm._config = cfg
    assert llm.choose_model(make_classified(to="2.13.17", latest="3.1.13")) == "full"
    assert llm.choose_model(make_classified(to="3.1.0", latest="3.1.13")) == "mini"
    assert llm.choose_model(make_classified(to="3.1.0", latest="3.1.13",
                                            grouped=True)) == "full"


# ── Foundry auth wiring (no network: construction only) ───────────────────

def _foundry_config(**over):
    from src.config import Config
    base = dict(github_token="x", repos=["o/r"],
                foundry_endpoint="https://my-res.services.ai.azure.com/api/projects/p")
    return Config(**{**base, **over})


def test_foundry_uses_api_key_and_never_asks_for_a_token(monkeypatch):
    from src.llm import llm_client
    monkeypatch.setattr(llm_client, "_entra_token_provider",
                        lambda scope: pytest.fail("should not need Entra ID with a key"))
    llm = llm_client.LLMClient(_foundry_config(foundry_api_key="k"),
                               executor=object())
    assert llm._client.api_key == "k"
    assert llm._client._azure_ad_token_provider is None
    assert str(llm._client.base_url).startswith(
        "https://my-res.services.ai.azure.com/openai/v1")


def test_blank_foundry_key_falls_back_to_entra_id(monkeypatch):
    from src.llm import llm_client
    asked = []
    monkeypatch.setattr(llm_client, "_entra_token_provider",
                        lambda scope: asked.append(scope) or (lambda: "tok"))
    llm = llm_client.LLMClient(_foundry_config(), executor=object())
    assert asked == ["https://cognitiveservices.azure.com/.default"]  # default scope
    # SDK sentinel: no key was supplied, so requests authenticate with the bearer token
    assert llm._client.api_key == "<missing API key>"
    assert llm._client._azure_ad_token_provider() == "tok"


def test_entra_scope_is_overridable(monkeypatch):
    from src.llm import llm_client
    asked = []
    monkeypatch.setattr(llm_client, "_entra_token_provider",
                        lambda scope: asked.append(scope) or (lambda: "tok"))
    llm_client.LLMClient(_foundry_config(foundry_token_scope="https://custom/.default"),
                         executor=object())
    assert asked == ["https://custom/.default"]


def test_foundry_without_resource_aborts():
    from src.llm.llm_client import LLMClient
    with pytest.raises(SystemExit):
        LLMClient(_foundry_config(foundry_endpoint="not-a-foundry-url"), executor=object())


# ── OpenAI request shaping ────────────────────────────────────────────────

def _client_for(monkeypatch, **over):
    from src.llm import llm_client
    monkeypatch.setattr(llm_client, "_entra_token_provider", lambda scope: (lambda: "tok"))
    return llm_client.LLMClient(_foundry_config(**over), executor=object())


def test_tools_use_openai_function_shape():
    from src.llm.tools import TOOL_DEFINITIONS
    names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    assert names == {"fetch_release_notes", "lookup_cve", "fetch_file_from_repo"}
    for tool in TOOL_DEFINITIONS:
        assert tool["type"] == "function"
        assert tool["function"]["parameters"]["type"] == "object"   # not input_schema


def test_rejected_temperature_is_dropped_then_remembered(monkeypatch):
    import openai
    llm = _client_for(monkeypatch)
    exc = openai.BadRequestError.__new__(openai.BadRequestError)
    Exception.__init__(exc, "Unsupported value: 'temperature' does not support 0")

    kwargs = {"model": "m", "temperature": 0, "max_completion_tokens": 10}
    assert llm._adapt("m", kwargs, exc) is True
    assert "temperature" not in kwargs                  # dropped for this call
    assert "temperature" not in llm._tuning_kwargs("m")  # and for every later call
    assert llm._adapt("m", kwargs, exc) is False        # never loops on the same param


def test_max_tokens_param_is_renamed_for_older_deployments(monkeypatch):
    import openai
    llm = _client_for(monkeypatch)
    exc = openai.BadRequestError.__new__(openai.BadRequestError)
    Exception.__init__(exc, "Unrecognized request argument: max_completion_tokens")

    kwargs = dict(llm._tuning_kwargs("m"), model="m")
    assert kwargs["max_completion_tokens"] == 2048
    assert llm._adapt("m", kwargs, exc) is True
    assert kwargs["max_tokens"] == 2048 and "max_completion_tokens" not in kwargs
    assert llm._tuning_kwargs("m")["max_tokens"] == 2048  # remembered


def test_tool_results_carry_call_ids(monkeypatch):
    from types import SimpleNamespace
    llm = _client_for(monkeypatch)
    llm._executor = SimpleNamespace(execute=lambda name, args: f"{name}:{args['vuln_id']}")
    call = SimpleNamespace(id="call_1", function=SimpleNamespace(
        name="lookup_cve", arguments='{"vuln_id": "CVE-1"}'))
    assert llm._tool_results(SimpleNamespace(tool_calls=[call])) == [
        {"role": "tool", "tool_call_id": "call_1", "content": "lookup_cve:CVE-1"}]


def test_malformed_tool_arguments_go_back_to_the_model(monkeypatch):
    from types import SimpleNamespace
    llm = _client_for(monkeypatch)
    call = SimpleNamespace(id="call_1", function=SimpleNamespace(
        name="lookup_cve", arguments="{not json"))
    [result] = llm._tool_results(SimpleNamespace(tool_calls=[call]))
    assert "Tool error" in result["content"]  # model can retry instead of the run dying


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
