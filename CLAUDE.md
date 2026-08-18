# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

AI-powered triage for Dependabot PRs across many repos. It collects open Dependabot
PRs (GitHub GraphQL), parses them into structured records, classifies them
deterministically, sends only the ambiguous ones to Claude for a verdict, and emits a
Markdown digest plus an append-only audit log.

**v1 is report-only.** It never comments on, closes, or merges a PR. Do not add write
paths to GitHub without an explicit request.

Product spec: `product.md`. Build plan/phases: `PLAN.md`. Azure swap list: `MIGRATION.md`.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env             # GITHUB_TOKEN and REPOS are required; run aborts without them

python main.py                   # full pipeline -> stdout + data/digest.md
python main.py --no-ai           # deterministic only, zero Claude calls (use while iterating)
python main.py --collect-only    # raw PR records as JSON
python main.py --grouped         # PRs grouped by (repo, ecosystem, manifest, dependency)

python -m pytest tests/ -q       # 51 tests, no network or API keys needed
```

`main.py` is a two-line shim over `src.main:main`.

## Pipeline

```
collector → deterministic classifier → Claude node (STALE_CANDIDATE only) → digest
```

| Stage | Module | Notes |
|---|---|---|
| Collect | `src/collector/github_client.py`, `pr_parser.py` | GraphQL + pagination; parses bump / requirement / group / monorepo-path titles and grouped-PR bodies |
| Classify | `src/classifier/deterministic.py` | Tags `CURRENT` / `DUPLICATE` / `STALE_CANDIDATE` / `UNKNOWN` with zero AI |
| Lookups | `registry_client.py` (PyPI, npm, NuGet), `osv_client.py` (batch CVE) | Stable releases only; pre-releases excluded |
| AI verdict | `src/llm/llm_client.py`, `prompts.py`, `tools.py` | `SUPERSEDED` / `STILL_VALID` / `NEEDS_HUMAN` |
| Persist | `src/storage/verdict_cache.py`, `audit_log.py` | Cross-repo cache; one JSON line per verdict |
| Report | `src/report/digest.py` | Markdown digest + Teams webhook payload |

All Pydantic models live in one file: `src/schemas.py`. All env-driven settings live in
one file: `src/config.py`.

## Invariants — do not break these

1. **Repo is the isolation boundary.** Duplicate detection only ever compares PRs within
   the same `(repo, manifest_path, dependency)`. Never across repos.
2. **Only `src/llm/llm_client.py` imports the `anthropic` SDK.** It is the single
   abstraction boundary for both providers. Nothing else touches the SDK.
3. **Deterministic first.** Claude only sees `STALE_CANDIDATE` records. If a check can be
   made from registry/OSV data, it belongs in `deterministic.py`, not the prompt.
4. **All AI tools are read-only** (`fetch_release_notes`, `lookup_cve`,
   `fetch_file_from_repo`) and every result is size-capped. Max 5 tool iterations per PR,
   then a final answer is forced via `tool_choice: none`.
5. **Never guess.** Thin or ambiguous evidence must return `NEEDS_HUMAN`. A wrong
   confident verdict is worse than deferring.
6. **Schema-validated output.** Every verdict passes `Verdict.model_validate_json`; one
   retry with the error fed back, then automatic `NEEDS_HUMAN`.
7. **Cost circuit breaker.** `LLM_MAX_CALLS_PER_RUN` (default 100) is checked before every
   call; exceeding it degrades to `NEEDS_HUMAN`, it does not raise.
8. **Version comparisons are PEP440/semver-aware** (`src/classifier/versions.py`). Never
   compare version strings lexically.
9. **Fail fast on config.** Missing required env vars `sys.exit` at startup.

## LLM specifics

- Provider switch: `LLM_PROVIDER=anthropic` (local) or `foundry` (Claude in Microsoft
  Foundry). Foundry accepts `FOUNDRY_ENDPOINT` (any portal URL — the resource host is
  derived by regex) or `FOUNDRY_RESOURCE`. Foundry auth is key **or** Entra ID, never
  both: a set `FOUNDRY_API_KEY` is used directly; a blank one builds an
  `azure_ad_token_provider` from `DefaultAzureCredential` (managed identity in Azure,
  `az login` locally). `azure-identity` is imported lazily, so key-based runs don't need it.
- Model routing: `model_default` (Haiku) for routine gaps; `model_escalation` (Sonnet) for
  grouped PRs or major-version gaps, plus a one-shot escalation when the default model
  returns `NEEDS_HUMAN` and budget remains.
- Claude 5 models reject `temperature`; the client detects the `BadRequestError`, caches the
  model in `_no_temperature`, and retries without it. Keep that fallback intact.
- Verdict cache key: `(ecosystem, dependency, to_ver, latest_ver, PROMPT_VERSION)`. **Bump
  `PROMPT_VERSION` in `src/llm/prompts.py` whenever you change the prompt** — otherwise
  stale verdicts are served from cache.
- Token counts (`input_tokens` / `output_tokens`), API calls, and cache hits are tracked on
  the client, surfaced in `RunStats`, the digest, and the structured logs. Keep new call
  sites accounted for.

## Conventions

- Python 3.13, stdlib-first, `httpx` for HTTP, `tenacity` for retries. Type hints use modern
  syntax (`str | None`, `list[str]`).
- Modules are small (40–190 lines) with a one-line docstring stating the module's job.
  Comments are sparse and explain *why*. Match that density.
- Structured logging only: `log_event(...)` from `src/log.py` writes JSON lines to stderr.
  Do not `print` outside the CLI output path.
- Tests use real Dependabot PR fixtures in `tests/fixtures/` and mock all network calls —
  the suite must keep running offline with no API keys.
- `.env` and `data/` are gitignored. Never commit tokens, digests, or audit logs.

## Azure target

`function_app/` is a thin timer-triggered wrapper around `src.main:run`. The migration is
config and adapters only — no pipeline logic changes. See `MIGRATION.md` before touching
storage or auth.
