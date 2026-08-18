"""Versioned system prompts. PROMPT_VERSION is logged on every verdict."""

import json

from src.schemas import ClassifiedPR, Verdict

PROMPT_VERSION = "2.0-gpt"  # bumped with the Azure OpenAI switch: retires Claude-era cache entries

SYSTEM_PROMPT = f"""You are a dependency-update triage analyst. You are given one open \
Dependabot PR that is STALE: a newer version of at least one of its dependencies exists \
beyond what the PR targets. Decide what the maintainers should do with this PR.

Verdicts:
- SUPERSEDED: the newer version fixes strictly more (especially security issues) with low \
breaking risk. Recommend recreating the PR to target it.
- STILL_VALID: the open PR is safe to merge now, and jumping further adds risk (major bump \
or breaking changes in the gap). Recommend merging as-is.
- NEEDS_HUMAN: release notes are missing or ambiguous, or the evidence is insufficient. \
NEVER guess — a confident wrong verdict is worse than deferring to a human.

Rules:
- Use the provided tools to read release notes, changelogs, and CVE details when the \
context given is not enough. Tools are read-only.
- Judge breaking risk from actual release notes, not from version numbers alone.
- A failing CI on the PR is context, not a verdict driver (the newer version may fix it).
- reasoning: 2-3 sentences maximum.

Output: a single JSON object matching this schema, and NOTHING else — no prose, no \
markdown fences:
{json.dumps(Verdict.model_json_schema(), indent=2)}
"""


def build_user_message(cp: ClassifiedPR) -> str:
    r = cp.record
    lines = [
        f"Repo: {r.repo}",
        f"PR #{r.pr_number}: {r.title}",
        f"Ecosystem: {r.ecosystem} | Manifest: {r.manifest_path}",
        f"PR age: {r.age_days} days | CI: {r.ci_status} | Mergeable: {r.mergeable}",
        f"Security-flagged: {r.is_security} | Grouped: {r.is_grouped}",
        "",
        "Dependency status (PR target vs latest stable):",
    ]
    for a in cp.assessments:
        gap = "BEHIND" if a.is_behind else "current"
        cves = f" | known CVEs in target: {', '.join(a.target_cves)}" if a.target_cves else ""
        lines.append(f"- {a.dependency}: {a.from_ver} -> {a.to_ver} "
                     f"(latest: {a.latest_ver}, {gap}){cves}")
    if cp.duplicate_overlaps:
        lines.append("")
        for o in cp.duplicate_overlaps:
            lines.append(f"Note: {o.dependency} is also bumped by newer open PR #{o.newer_pr}.")
    if r.release_notes_excerpt:
        lines += ["", "Release notes from the PR body (target version):",
                  r.release_notes_excerpt]
    if r.changelog_links:
        lines += ["", "Changelog links: " + " ".join(r.changelog_links)]
    lines += ["", "Give your verdict as JSON."]
    return "\n".join(lines)
