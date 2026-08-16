# Dependabot PR Classifier

## About

Dependabot PR Classifier is an AI-centric triage system that keeps dependency update PRs from piling up and going stale. It scans open Dependabot PRs across all configured repositories, parses each one into structured data (dependency, version jump, ecosystem, manifest), and classifies it deterministically: is it current, a duplicate of a newer PR, or stale because the registry already has a newer version? Only the stale, ambiguous cases reach Claude — which reads real release notes and CVE data through read-only tools, then returns a structured verdict: SUPERSEDED (recreate the PR), STILL_VALID (merge as-is), or NEEDS_HUMAN (defer, never guess).

The result is a single weekly digest a maintainer can act on in minutes: every PR with its status, risk level, and an exact copy-pasteable action such as `@dependabot recreate`. Verdicts are cached across repositories — fourteen repos with the same axios bump cost one AI call — and every verdict lands in an append-only audit log, building the evidence base for future automation. Version 1 is strictly report-only: it never comments on, closes, or merges any PR. Built locally against the Anthropic API, it migrates to Azure (Claude in Microsoft Foundry, Functions, Table Storage) with config changes only.

## Features

1. **Multi-repo collection** — GitHub GraphQL collector with pagination; scans every configured repo in one run.
2. **Full title/body parsing** — handles all Dependabot variants: plain bumps, monorepo paths (`in /frontend`), grouped PRs (per-dependency detail extracted from the body), requirement updates, and conventional-commit prefixes.
3. **Deterministic-first classification** — CURRENT / DUPLICATE / STALE_CANDIDATE / UNKNOWN tags computed from registry data with zero AI involvement; a `--no-ai` mode runs the whole report this way.
4. **AI verdicts with investigation** — Claude fetches release notes, changelogs, and CVE details itself (read-only tools) before judging breaking risk in the version gap.
5. **Cost-aware model routing** — Haiku for routine verdicts; automatic escalation to Sonnet for major-version gaps, grouped PRs, or low-confidence first passes.
6. **Cross-repo verdict cache** — keyed on (ecosystem, dependency, target version, latest version, prompt version); identical bumps in other repos are free.
7. **Weekly digest** — single Markdown page with needs-human items first, org summary, cross-repo rollup, and per-repo action tables; Teams webhook payload ready.
8. **Append-only audit log** — every verdict recorded with model, prompt version, token counts, and cache status.
9. **Token monitoring** — each run reports total tokens consumed (input/output breakdown), Claude API calls, and cache hits directly in the digest and structured logs, making per-run cost visible at a glance.
10. **Multi-provider LLM** — one config switch runs the same pipeline against the Anthropic API or Claude in Microsoft Foundry (Azure), with no code changes.
11. **Azure-ready** — Timer-triggered Function wrapper and a documented migration path (`MIGRATION.md`) with no logic changes.

## Guardrails

1. **Report-only** — v1 never writes to any PR: no comments, no closes, no merges.
2. **Read-only AI tools** — Claude can only fetch release notes, CVE details, and files; every tool result is size-capped.
3. **Tool loop cap** — maximum 5 tool iterations per PR, then a final answer is forced.
4. **Hard cost circuit breaker** — configurable cap (default 100) on Claude API calls per run.
5. **Schema-validated output** — every verdict must pass Pydantic validation; one retry with the error fed back, then automatic NEEDS_HUMAN.
6. **Never-guess rule** — thin or ambiguous evidence yields NEEDS_HUMAN by design; a wrong confident verdict is treated as worse than deferring.
7. **Repo isolation** — duplicate detection never crosses repositories or manifests; only package-level facts are shared via the cache.
8. **Fail-fast config** — missing required environment variables abort the run at startup.
9. **Secrets hygiene** — `.env` and all run outputs are gitignored; the client environment uses Key Vault / managed identity instead.

## Output

| Output | Path | Description |
|--------|------|-------------|
| Weekly digest | `data/digest.md` (also printed to stdout) | The human-readable report: statuses, risks, actions per PR |
| Audit log | `data/audit_log.jsonl` | Append-only, one JSON line per AI verdict (model, prompt version, tokens, cache hit) |
| Verdict cache | `data/verdict_cache.json` | Cross-repo AI verdict reuse |
| Structured logs | stderr (JSON lines) | Run telemetry: PRs collected, classification counts, API calls, tokens |
| Raw JSON | stdout via `--collect-only` / `--grouped` | Machine-readable PR records for downstream use |

## Checks performed

1. **PR staleness check** — the PR's target version is compared against the latest stable release on the registry (PyPI, npm, NuGet); pre-releases excluded.
2. **Duplicate check** — more than one open PR for the same (repo, manifest, dependency) is detected; only the newest survives, partial group overlaps are flagged without over-closing.
3. **Build/CI check** — each PR's status-check rollup (pass/fail) and mergeability (conflicts) are captured and factored into the recommended action.
4. **Vulnerability check** — the PR's *target* version is queried against OSV.dev; known CVEs in the target are a strong stale signal, recorded by ID.
5. **Breaking-change check** — release notes across the version gap are read (by Claude) to judge upgrade risk from evidence, not version numbers alone.
6. **Security-update detection** — security-flagged PRs identified via labels and body markers.
7. **Version sanity** — comparisons use PEP440/semver-aware logic (`1.10.0 > 1.9.0`), never string comparison; requirement operators (`~=2.31`) normalized.
8. **PR age tracking** — days-open per PR, with the oldest surfaced in the digest summary.
