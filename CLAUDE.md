# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

AI-powered triage for Dependabot PRs across many repos. It collects open Dependabot
PRs (GitHub GraphQL), parses them into structured records, classifies them
deterministically, sends only the ambiguous ones to the model for a verdict, and emits a
Markdown digest plus an append-only audit log.

This branch is the **Microsoft-native** build: GPT deployments on Azure OpenAI
(Foundry resource), Entra ID auth, Azure Functions. There is no Anthropic code path.

**v1 is report-only.** It never comments on, closes, or merges a PR. Do not add write
paths to GitHub without an explicit request.

Product spec: `product.md`. Build plan/phases: `PLAN.md`. Azure swap list: `MIGRATION.md`.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env             # GITHUB_TOKEN and REPOS are required; run aborts without them

python ghe_token_test.py         # pre-flight: GitHub token + repo access
python scripts/smoke_test.py     # pre-flight: endpoint, auth, deployment names
python main.py                   # full pipeline -> stdout + data/digest.md
python main.py --no-ai           # deterministic only, zero LLM calls (use while iterating)
python main.py --collect-only    # raw PR records as JSON
python main.py --grouped         # PRs grouped by (repo, ecosystem, manifest, dependency)

python -m pytest tests/ -q       # 61 tests, no network or API keys needed
```

`main.py` is a two-line shim over `src.main:main`.

## Pipeline

```
collector → deterministic classifier → GPT node (STALE_CANDIDATE only) → digest
```

| Stage | Module | Notes |
|---|---|---|
| Collect | `src/collector/github_client.py`, `pr_parser.py` | GraphQL + pagination; parses bump / requirement / group / monorepo-path titles and grouped-PR bodies |
| Classify | `src/classifier/deterministic.py` | Tags `CURRENT` / `DUPLICATE` / `STALE_CANDIDATE` / `UNKNOWN` with zero AI |
| Lookups | `registry_client.py` (PyPI, npm, NuGet), `osv_client.py` (batch CVE) | Stable releases only; pre-releases excluded |
| AI verdict | `src/llm/llm_client.py`, `prompts.py`, `tools.py` | Azure OpenAI chat completions + tool loop → `SUPERSEDED` / `STILL_VALID` / `NEEDS_HUMAN` |
| Persist | `src/storage/verdict_cache.py`, `audit_log.py` | Cross-repo cache; one JSON line per verdict |
| Report | `src/report/digest.py` | Markdown digest + Teams webhook payload |

All Pydantic models live in one file: `src/schemas.py`. All env-driven settings live in
one file: `src/config.py`.

## Invariants — do not break these

1. **Repo is the isolation boundary.** Duplicate detection only ever compares PRs within
   the same `(repo, manifest_path, dependency)`. Never across repos.
2. **Only `src/llm/llm_client.py` imports the `openai` SDK.** It is the single
   abstraction boundary. Nothing else touches the SDK, and nothing imports `anthropic`.
3. **Deterministic first.** The model only sees `STALE_CANDIDATE` records. If a check can
   be made from registry/OSV data, it belongs in `deterministic.py`, not the prompt.
4. **All AI tools are read-only** (`fetch_release_notes`, `lookup_cve`,
   `fetch_file_from_repo`) and every result is size-capped. Max 5 tool iterations per PR,
   then a final answer is forced via `tool_choice="none"`.
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

- **Azure OpenAI only.** `LLMClient` builds a plain `openai.OpenAI` against
  `https://<resource>.services.ai.azure.com/openai/v1`, where `<resource>` is derived by
  regex from `FOUNDRY_ENDPOINT` (any portal URL works) or set via `FOUNDRY_RESOURCE`.
  **Do not switch to `openai.AzureOpenAI`** — it appends `?api-version=...`, and the
  Foundry v1 route answers that with `404 Resource not found`.
- **Auth is key or Entra ID.** A set `FOUNDRY_API_KEY` is passed as the api_key; a blank
  one installs `_EntraAuth` (an `httpx.Auth`) on the transport, which stamps a fresh
  `DefaultAzureCredential` bearer token on every request. The transport is used because
  the plain client only accepts a static key string — passing the token *provider* as
  `api_key`, as the portal sample does, sends `Bearer <function ...>` and 401s.
  `azure-identity` is imported lazily, so key-based runs never need it.
- **Token scope is `https://ai.azure.com/.default`**, not the classic
  `cognitiveservices.azure.com` audience. Override with `FOUNDRY_TOKEN_SCOPE`.
- **Data-plane RBAC is separate from control plane.** Subscription Owner does *not* grant
  inference access; the identity needs Cognitive Services OpenAI User on the resource.
  `scripts/smoke_test.py` distinguishes this (401 PermissionDenied) from a missing
  deployment (404).
- **Model names are Azure deployment names**, not catalogue model IDs. `model_default` and
  `model_escalation` may point at the same deployment; escalation then no-ops by design.
- **Parameter self-healing.** Deployments disagree on `temperature` and
  `max_tokens`/`max_completion_tokens`. `_adapt()` drops or renames the rejected param on a
  `BadRequestError` and records it in `_unsupported[model]`, so each param is adapted once
  per deployment and every later call skips it. Keep this — it is what makes one code path
  work across model generations.
- **Tool loop shapes** (OpenAI, not Anthropic): tools are
  `{"type": "function", "function": {...}}`; continue while `message.tool_calls` is
  non-empty; append the assistant message verbatim via `model_dump(exclude_none=True)`;
  reply with `{"role": "tool", "tool_call_id": ...}` per call. Tool arguments arrive as a
  JSON *string* — malformed JSON is returned to the model as text, never raised.
- **Verdict cache key**: `(ecosystem, dependency, to_ver, latest_ver, PROMPT_VERSION)`.
  **Bump `PROMPT_VERSION` in `src/llm/prompts.py` whenever you change the prompt** —
  otherwise stale verdicts are served. It is at `2.0-gpt`; the bump retired the
  Claude-era entries, since the key does not include the model.
- **Token counts** come from `usage.prompt_tokens` / `usage.completion_tokens` and feed
  `RunStats`, the digest, and the structured logs. Keep new call sites accounted for.

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
