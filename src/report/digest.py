"""Weekly Markdown digest (Jinja2) + Teams webhook payload.

Section order is deliberate: needs-human first (the interesting ones),
then org summary, cross-repo rollup, per-repo action tables.
"""

from datetime import date

import httpx
from jinja2 import Template

from src.schemas import ClassifiedPR, RunStats

_TEMPLATE = Template("""\
# Dependabot Triage Digest — {{ today }}

## Needs human ({{ needs_human | length }})
{% for cp in needs_human -%}
- [{{ cp.record.repo }}#{{ cp.record.pr_number }}]({{ cp.record.url }}) {{ cp.record.title }} — {{ cp.verdict.reasoning if cp.verdict else "no registry data / unparseable" }}
{% else -%}
Nothing needs a human this week.
{% endfor %}
## Summary
- Repos scanned: **{{ n_repos }}** | Open Dependabot PRs: **{{ n_prs }}**
- Stale: **{{ counts.get("STALE_CANDIDATE", 0) }}** | Duplicates: **{{ counts.get("DUPLICATE", 0) }}** | Current: **{{ counts.get("CURRENT", 0) }}** | Unknown: **{{ counts.get("UNKNOWN", 0) }}**
- Oldest open PR: **{{ oldest_days }} days**
{% if stats %}
## Token usage
- Total tokens this run: **{{ "{:,}".format(stats.total_tokens) }}** ({{ "{:,}".format(stats.input_tokens) }} in / {{ "{:,}".format(stats.output_tokens) }} out){% if stats.model %} on `{{ stats.model }}`{% endif %}
- Claude API calls: **{{ stats.api_calls }}** | Stale PRs analyzed: **{{ stats.stale_analyzed }}** | Cache hits: **{{ stats.cache_hits }}** (reused prior verdicts, 0 tokens)
{% endif %}
## Cross-repo rollup
{% for line in rollup -%}
- {{ line }}
{% else -%}
- No dependency is pending in more than one repo.
{% endfor %}
## Actions by repo
{% for repo, rows in by_repo.items() %}
### {{ repo }}

| PR | Dependency | Target → Latest | Age | CI | Status | Risk | Action |
|----|------------|-----------------|-----|----|--------|------|--------|
{% for r in rows -%}
| [#{{ r.pr }}]({{ r.url }}) | {{ r.dep }} | {{ r.versions }} | {{ r.age }}d | {{ r.ci }} | {{ r.status }} | {{ r.risk }} | {{ r.action }} |
{% endfor %}
{%- endfor %}
""")


def action_for(cp: ClassifiedPR) -> str:
    if cp.status == "DUPLICATE":
        newer = sorted({o.newer_pr for o in cp.duplicate_overlaps})
        return f"Close — superseded by #{', #'.join(map(str, newer))}"
    if cp.status == "CURRENT":
        return ("Merge (after fixing CI)" if cp.record.ci_status == "FAILURE" else "Merge")
    if cp.status == "UNKNOWN":
        return "Manual review (no version data)"
    v = cp.verdict
    if v is None:
        return "Manual review (AI skipped)"
    if v.verdict == "SUPERSEDED":
        return f"`@dependabot recreate` — {v.recommended_action}"
    if v.verdict == "STILL_VALID":
        return "Merge as-is"
    return "Manual review"


def _dep_cell(cp: ClassifiedPR) -> tuple[str, str]:
    """(dependency column, versions column)."""
    if cp.record.is_grouped:
        dep = "group: " + ", ".join(a.dependency for a in cp.assessments)
    else:
        dep = cp.record.dependency or "?"
    behind = [a for a in cp.assessments if a.is_behind]
    if behind:
        versions = "; ".join(f"{a.to_ver} → {a.latest_ver}" for a in behind)
    elif cp.assessments:
        versions = ", ".join(a.to_ver or "?" for a in cp.assessments) + " (latest)"
    else:
        versions = "?"
    return dep, versions


def _rollup(results: list[ClassifiedPR], n_repos: int) -> list[str]:
    pending: dict[tuple[str, str], set[str]] = {}
    cves: dict[tuple[str, str], set[str]] = {}
    for cp in results:
        for a in cp.assessments:
            key = (cp.record.ecosystem or "?", a.dependency)
            if a.is_behind:
                pending.setdefault(key, set()).add(cp.record.repo)
            for cve in a.target_cves:
                cves.setdefault((a.dependency, cve), set()).add(cp.record.repo)
    lines = [f"{dep} ({eco}): newer version pending in {len(repos)}/{n_repos} repos"
             for (eco, dep), repos in sorted(pending.items()) if len(repos) > 1]
    lines += [f"{dep} {cve}: target version still vulnerable in {len(repos)}/{n_repos} repos"
              for (dep, cve), repos in sorted(cves.items())]
    return lines


def render_digest(results: list[ClassifiedPR], stats: RunStats | None = None) -> str:
    repos = sorted({cp.record.repo for cp in results})
    counts: dict[str, int] = {}
    by_repo: dict[str, list] = {}
    for cp in sorted(results, key=lambda c: (c.record.repo, c.record.pr_number)):
        counts[cp.status] = counts.get(cp.status, 0) + 1
        dep, versions = _dep_cell(cp)
        by_repo.setdefault(cp.record.repo, []).append(type("Row", (), {
            "pr": cp.record.pr_number, "url": cp.record.url, "dep": dep,
            "versions": versions, "age": cp.record.age_days,
            "ci": {"SUCCESS": "✅", "FAILURE": "❌"}.get(cp.record.ci_status, "–"),
            "status": cp.status + (" → " + cp.verdict.verdict if cp.verdict else ""),
            "risk": cp.verdict.risk_of_newer_version if cp.verdict else "–",
            "action": action_for(cp),
        }))
    needs_human = [cp for cp in results
                   if (cp.verdict and cp.verdict.verdict == "NEEDS_HUMAN")
                   or cp.status == "UNKNOWN"]
    return _TEMPLATE.render(
        today=date.today().isoformat(),
        needs_human=needs_human,
        n_repos=len(repos),
        n_prs=len(results),
        counts=counts,
        oldest_days=max((cp.record.age_days for cp in results), default=0),
        stats=stats,
        rollup=_rollup(results, len(repos)),
        by_repo=by_repo,
    )


def to_teams_payload(markdown: str) -> dict:
    """MessageCard payload for a Teams incoming webhook."""
    return {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": "Dependabot Triage Digest",
        "themeColor": "6264A7",
        "text": markdown,
    }


def post_to_teams(webhook_url: str, markdown: str) -> None:
    httpx.post(webhook_url, json=to_teams_payload(markdown), timeout=20).raise_for_status()
