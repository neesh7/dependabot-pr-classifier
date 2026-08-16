import json

from src.report.digest import action_for, render_digest, to_teams_payload
from src.schemas import (ClassifiedPR, DuplicateOverlap, RunStats, UpdateAssessment,
                         Verdict, VerdictMeta)
from src.storage.audit_log import AuditLog
from tests.test_deterministic import make_record


def classified(pr, dep, to, status, *, latest=None, verdict=None, overlaps=(),
               ci="SUCCESS", repo="o/r") -> ClassifiedPR:
    rec = make_record(pr, dep, to, repo=repo).model_copy(update={"ci_status": ci})
    return ClassifiedPR(
        record=rec, status=status,
        duplicate_overlaps=list(overlaps),
        assessments=[UpdateAssessment(dependency=dep, from_ver="1.0", to_ver=to,
                                      latest_ver=latest or to,
                                      is_behind=bool(latest and latest != to))],
        verdict=verdict,
        verdict_meta=VerdictMeta(model="m", prompt_version="1.0") if verdict else None,
    )


def v(kind, action="x", risk="low"):
    return Verdict(verdict=kind, recommended_action=action,
                   risk_of_newer_version=risk, reasoning="because")


# ── action column ─────────────────────────────────────────────────────────

def test_actions():
    dup = classified(1, "a", "1.0", "DUPLICATE",
                     overlaps=[DuplicateOverlap(dependency="a", newer_pr=9)])
    assert action_for(dup) == "Close — superseded by #9"
    assert action_for(classified(2, "b", "1.0", "CURRENT")) == "Merge"
    assert action_for(classified(3, "c", "1.0", "CURRENT",
                                 ci="FAILURE")) == "Merge (after fixing CI)"
    sup = classified(4, "d", "1.0", "STALE_CANDIDATE", latest="2.0",
                     verdict=v("SUPERSEDED", "recreate to 2.0"))
    assert action_for(sup) == "`@dependabot recreate` — recreate to 2.0"
    assert action_for(classified(5, "e", "1.0", "STALE_CANDIDATE", latest="1.1",
                                 verdict=v("STILL_VALID"))) == "Merge as-is"
    assert action_for(classified(6, "f", "1.0", "STALE_CANDIDATE", latest="1.1",
                                 verdict=v("NEEDS_HUMAN"))) == "Manual review"


# ── digest rendering ──────────────────────────────────────────────────────

def test_digest_sections_and_rows():
    results = [
        classified(10, "axios", "1.6.0", "STALE_CANDIDATE", latest="1.7.2",
                   verdict=v("SUPERSEDED", "recreate to 1.7.2", "low")),
        classified(11, "lodash", "4.17.21", "CURRENT"),
        classified(12, "left-pad", "1.0.0", "STALE_CANDIDATE", latest="2.0.0",
                   verdict=v("NEEDS_HUMAN", "manual review", "medium")),
    ]
    stats = RunStats(stale_analyzed=2, cache_hits=0, api_calls=3,
                     input_tokens=8000, output_tokens=1233, model="claude-opus-5")
    md = render_digest(results, stats)
    # needs-human comes before the summary, summary before tables
    assert md.index("## Needs human") < md.index("## Summary") < md.index("## Actions by repo")
    assert "o/r#12" in md.split("## Summary")[0]        # needs-human links the right PR
    assert "[#10](http://x)" in md                       # table row with link
    assert "1.6.0 → 1.7.2" in md
    assert "`@dependabot recreate` — recreate to 1.7.2" in md
    assert "Open Dependabot PRs: **3**" in md
    # token usage section
    assert "## Token usage" in md
    assert "Total tokens this run: **9,233**" in md
    assert "8,000 in / 1,233 out" in md
    assert "claude-opus-5" in md


def test_digest_omits_token_usage_without_stats():
    results = [classified(11, "lodash", "4.17.21", "CURRENT")]
    md = render_digest(results)  # no-ai run
    assert "## Token usage" not in md


def test_cross_repo_rollup_counts_repos():
    results = [
        classified(1, "axios", "1.6.0", "STALE_CANDIDATE", latest="1.7.2", repo="o/r1"),
        classified(2, "axios", "1.6.0", "STALE_CANDIDATE", latest="1.7.2", repo="o/r2"),
    ]
    md = render_digest(results)
    assert "pending in 2/2 repos" in md


def test_teams_payload_is_messagecard():
    p = to_teams_payload("# hi")
    assert p["@type"] == "MessageCard" and p["text"] == "# hi"


# ── audit log ─────────────────────────────────────────────────────────────

def test_audit_log_appends_verdicts_only(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(str(path))
    log.append(classified(1, "a", "1.0", "CURRENT"))          # no verdict -> skipped
    log.append(classified(2, "b", "1.0", "STALE_CANDIDATE", latest="2.0",
                          verdict=v("SUPERSEDED")))
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["pr"] == 2
    assert entry["verdict"] == "SUPERSEDED"
    assert entry["model"] == "m" and entry["prompt_version"] == "1.0"
    assert "timestamp" in entry and "cache_hit" in entry
