# Dependabot AI Triage — Build Plan

**Goal:** AI-centric system that scans open Dependabot PRs across ~30 repos, detects stale/superseded/duplicate PRs, and produces a report-only weekly digest with recommended actions (`@dependabot recreate` / close / merge).

**Strategy:** Build locally against personal repos + Anthropic API → swap config to client env (Azure Functions + Claude in Microsoft Foundry + Entra ID + Key Vault + Table Storage). Zero logic changes at migration — only adapters/config.

**AI model:** Claude only. Haiku for routine version-gap verdicts, Sonnet for major bumps / thin-context cases. Both available in Foundry, so routing survives migration.

---

## Architecture

```
┌─────────────┐   ┌──────────────────┐   ┌─────────────────┐   ┌──────────┐
│  Collector  │──▶│  Deterministic   │──▶│  Claude Node    │──▶│  Report  │
│  (GraphQL)  │   │  Classifier      │   │  (tool-use)     │   │  Digest  │
└─────────────┘   └──────────────────┘   └─────────────────┘   └──────────┘
      │              CURRENT / DUPLICATE      only STALE_          Teams +
      │              / STALE_CANDIDATE        CANDIDATEs           audit log
      ▼
  (repo) → (ecosystem, manifest, dependency) → [PRs]
```

**Design rules (non-negotiable):**
1. Repo is the isolation boundary for verdicts. Duplicates = same repo + same manifest + same dep only.
2. AI verdicts are cached cross-repo on `(ecosystem, dep, to_ver, latest_ver)` — package-level facts don't depend on the repo.
3. Deterministic checks run first; Claude only sees what logic can't resolve.
4. All Claude calls go through ONE module (`llm/llm_client.py`). Nothing else imports the SDK.
5. All tools in the Claude loop are READ-ONLY. Max 5 tool iterations per PR.
6. v1 is report-only. No writes to any PR.

---

## Repo structure

```
dependabot-ai-triage/
├── src/
│   ├── collector/
│   │   ├── github_client.py      # GraphQL client, auth (PAT now, GitHub App later)
│   │   └── pr_parser.py          # PR title/body → structured record
│   ├── classifier/
│   │   ├── deterministic.py      # duplicate + staleness tagging
│   │   ├── registry_client.py    # latest-version lookup per ecosystem
│   │   └── osv_client.py         # OSV.dev batch CVE lookups
│   ├── llm/
│   │   ├── llm_client.py         # THE abstraction boundary (anthropic | foundry)
│   │   ├── prompts.py            # versioned system prompts (PROMPT_VERSION const)
│   │   ├── schemas.py            # Pydantic verdict models
│   │   └── tools.py              # tool definitions + tool-use loop
│   ├── report/
│   │   └── digest.py             # Markdown digest + Teams webhook payload
│   ├── storage/
│   │   ├── audit_log.py          # append-only verdict log (JSON now, Table later)
│   │   └── verdict_cache.py      # cross-repo AI verdict cache
│   ├── config.py                 # all env-driven settings in one place
│   └── main.py                   # orchestrator: collect → classify → analyze → report
├── function_app/                 # Azure Functions thin wrapper (Phase 5)
├── tests/
│   ├── fixtures/                 # REAL Dependabot PR bodies/titles as test data
│   ├── test_pr_parser.py
│   ├── test_deterministic.py
│   └── test_schemas.py
├── .env.example
├── requirements.txt
├── MIGRATION.md                  # exact swap list for client env
└── PLAN.md                       # this file
```

**requirements.txt starting point:**
```
anthropic
pydantic>=2
httpx
python-dotenv
tenacity          # retries/backoff
jinja2            # digest templating
pytest
```

---

## Phase 0 — Test lab setup (half day)

- [ ] Pick 2–3 personal GitHub repos (or create fresh ones)
- [ ] Pin intentionally outdated deps: e.g. `requests==2.25.0`, `lodash@4.17.15`, an old `axios` with a known CVE
- [ ] Add `.github/dependabot.yml` to each (weekly schedule, ecosystems: pip + npm to start)
- [ ] Let Dependabot raise PRs — do NOT merge them; this is your test data
- [ ] To simulate a "stale" PR: pin a dep 3+ versions behind, let Dependabot PR an intermediate version manually via an old commit, or just let a PR sit while newer releases exist
- [ ] Create a GitHub PAT (classic, `repo` scope) — note in MIGRATION.md: client env uses a GitHub App
- [ ] Confirm Anthropic API key works: one Haiku ping
- [ ] `.env` file: `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `LLM_PROVIDER=anthropic`, `REPOS=owner/repo1,owner/repo2`

**Ask the team in parallel:** which ecosystems do the 30 client repos actually use? (Likely npm + NuGet + pip.) Build only those registry clients in Phase 2.

---

## Phase 1 — Collector (Day 1–2)

- [ ] `github_client.py`: GraphQL query — open PRs where author = `dependabot[bot]`
      Fields: number, title, body, createdAt, mergeable, statusCheckRollup (CI state), files (paths), baseRefName, url
- [ ] Paginate properly (repos with 30+ open PRs exist)
- [ ] `pr_parser.py`: parse titles into `(dependency, from_version, to_version)`
      Handle ALL variants — collect real samples into `tests/fixtures/`:
      - `Bump lodash from 4.17.20 to 4.17.21`
      - `Bump lodash from 4.17.20 to 4.17.21 in /frontend` (manifest path)
      - `Bump the npm_and_yarn group with 3 updates` (grouped)
      - `Update requests requirement from ~=2.25 to ~=2.31`
      - Security PRs (identifiable via body markers / labels)
- [ ] Extract from body: release-notes section, changelog links, compatibility score
- [ ] Output record: `{repo, ecosystem, manifest_path, dependency, from_ver, to_ver, pr_number, url, age_days, ci_status, mergeable, is_security, is_grouped, body_excerpt}`
- [ ] Group: `(repo) → (ecosystem, manifest_path, dependency) → [records]`
- [ ] Unit tests on fixtures for every title variant

**Milestone:** `python -m src.main --collect-only` dumps clean JSON of all Dependabot PRs across your test repos.

---

## Phase 2 — Deterministic classifier (Day 2–3)

- [ ] `deterministic.py` — tag each PR:
      - `DUPLICATE`: >1 open PR for same (repo, manifest, dep) → all but newest tagged
      - `STALE_CANDIDATE`: latest registry version > PR's to_version
      - `CURRENT`: to_version == latest stable
- [ ] `registry_client.py` — latest STABLE version lookups (skip pre-releases):
      - PyPI: `https://pypi.org/pypi/{pkg}/json`
      - npm: `https://registry.npmjs.org/{pkg}` (use `dist-tags.latest`)
      - NuGet: flat container API `/v3-flatcontainer/{pkg}/index.json`
      - (add others only if client repos need them)
      - In-memory cache per run — same package appears across many repos
- [ ] `osv_client.py` — OSV.dev batch API: known CVEs for `(ecosystem, pkg, version)`
      - Check the PR's TARGET version: if it has known CVEs → strong stale signal, record CVE IDs
- [ ] Version comparison: use `packaging.version` for PyPI, semver logic for npm/NuGet — do NOT string-compare
- [ ] Handle grouped PRs: a grouped PR is stale if ANY member dep has a newer version; record per-dep detail
- [ ] Unit tests: duplicate detection, monorepo (same dep, two manifests = NOT duplicates), version edge cases (`1.10.0 > 1.9.0`)

**Milestone:** pipeline produces a full report with zero AI calls. This alone is demo-able to the team.

---

## Phase 3 — Claude classification node (Day 3–5)

The core AI work. Only `STALE_CANDIDATE`s reach this stage.

- [ ] `schemas.py` — Pydantic verdict model:
      ```python
      class Verdict(BaseModel):
          verdict: Literal["SUPERSEDED", "STILL_VALID", "NEEDS_HUMAN"]
          recommended_action: str          # "recreate to 4.18.2" | "merge as-is" | "manual review"
          risk_of_newer_version: Literal["low", "medium", "high"]
          breaking_changes_in_gap: list[str]
          additional_cves_fixed: list[str]
          reasoning: str                   # 2–3 sentences max
      ```
- [ ] `prompts.py` — system prompt, with `PROMPT_VERSION = "1.0"` logged on every verdict.
      Core logic:
      - SUPERSEDED: newer version fixes strictly more (esp. security) with low breaking risk → recreate
      - STILL_VALID: open PR is safe to merge now; jumping further adds risk (major bump / breaking changes in gap)
      - NEEDS_HUMAN: release notes missing/ambiguous — NEVER guess. Confident wrong verdicts kill v1 trust.
      - Output: JSON only, matching the schema. No prose outside JSON.
- [ ] `llm_client.py` — provider switch:
      ```python
      # LLM_PROVIDER=anthropic → anthropic.Anthropic(api_key=...)
      # LLM_PROVIDER=foundry   → same Messages API; Foundry base_url + Entra ID token
      #                          (fill exact constructor from MS docs at migration)
      ```
      Model routing: `claude-haiku-*` default; escalate to `claude-sonnet-*` when:
      major-version gap, OR grouped PR, OR first pass returned NEEDS_HUMAN with tool budget remaining
- [ ] `tools.py` — read-only tools for the Claude tool-use loop:
      - `fetch_release_notes(package, ecosystem, from_ver, to_ver)` → GitHub Releases API / registry metadata
      - `lookup_cve(cve_id)` → OSV.dev detail
      - `fetch_file_from_repo(repo, path)` → e.g. CHANGELOG.md (read-only, size-capped)
      - Loop cap: 5 iterations, then force final answer
- [ ] Context bundle per call: dep, ecosystem, PR target ver vs latest, release notes in the gap (pre-fetched when easy), OSV data for both versions, PR age + CI status
- [ ] Validation retry: on Pydantic failure, ONE retry with the validation error fed back → else NEEDS_HUMAN
- [ ] Temperature 0
- [ ] `verdict_cache.py` — cache key `(ecosystem, dep, to_ver, latest_ver, prompt_version)`:
      check before calling Claude; 14 repos with the same axios bump = 1 call
- [ ] Retry/backoff (tenacity) on 429/5xx for both GitHub and Anthropic calls
- [ ] Cost guard: log token usage per run; hard cap on calls per run (e.g. 100) as a circuit breaker

**Milestone:** stale PRs in your test repos get sensible verdicts; cache hit-rate visible in logs.

---

## Phase 4 — Report + audit log (Day 5–6)

- [ ] `digest.py` — Markdown (Jinja2 template), sections in this order:
      1. **Needs human** (top — these are the interesting ones)
      2. **Org summary**: N repos scanned, X open Dependabot PRs, Y stale, Z duplicates, oldest PR age
      3. **Cross-repo rollup**: "axios CVE-XXXX pending in 14/30 repos" style lines; note repos where the same bump already merged cleanly (confidence signal)
      4. **Per-repo action table**: PR link | dep | target ver → latest | verdict | risk | exact command (`@dependabot recreate` / close dup / merge)
- [ ] Teams webhook payload (Adaptive Card or simple MessageCard). Locally: render Markdown to file; wire real webhook at migration
- [ ] `audit_log.py` — append-only, one JSON line per verdict:
      `{timestamp, repo, pr, dep, versions, verdict, model, prompt_version, tokens, cache_hit}`
      This log is the evidence base for graduating to v2 automation ("AI said SUPERSEDED 84×, humans agreed 81×")

**Milestone:** one command produces the full weekly digest for your test repos.

---

## Phase 5 — Hardening + migration prep (Day 6–7)

- [ ] Structured logging (JSON logs) — maps to Application Insights later
- [ ] `function_app/` — Timer-triggered Azure Function wrapper calling `src.main.run()`; test locally with Azure Functions Core Tools
- [ ] Config validation on startup (fail fast on missing env)
- [ ] `MIGRATION.md` — the exact swap list:

| Local (build env)              | Client env                                            |
|--------------------------------|-------------------------------------------------------|
| Anthropic API + API key        | Claude in Microsoft Foundry (Azure-hosted, GA)        |
| API key in `.env`              | Entra ID managed identity / Key Vault                 |
| GitHub PAT                     | GitHub App (org-installed; PRs:read, contents:read)   |
| `python -m src.main` / cron    | Azure Function, Timer trigger (weekly)                |
| Local JSON audit log + cache   | Azure Table Storage                                   |
| Markdown file output           | Teams incoming webhook                                |
| Console logs                   | Application Insights                                  |

- [ ] Verify client's Azure region supports Global Standard deployment for chosen Claude models BEFORE committing in the design doc
- [ ] Note for client doc: Foundry has no built-in content filtering for Claude — call out Azure AI Content Safety as optional (low relevance for changelog analysis); Anthropic AUP compliance applies
- [ ] Billing note for client doc: Claude in Foundry bills via CCUs on Azure invoice; counts toward MACC for eligible customers

---

## Phase 6 — Private registry / Azure Artifacts feed support (planned, not built)

**Problem:** "stale" is currently defined against public registries (PyPI/npm/NuGet).
If a client's repos resolve from an Azure Artifacts feed, that definition breaks two ways:
1. **False staleness** — upstream has a newer version but the curated feed hasn't approved
   it; recommending `@dependabot recreate` to a version the feed can't resolve produces a
   broken PR (confidently-wrong advice = trust killer).
2. **Internal packages invisible** — private packages don't exist on public registries, so
   lookups return None and those PRs degrade to UNKNOWN.

**Why it's cheap:** `registry_client.py` is already the single lookup seam, and Azure
Artifacts speaks the same protocols (NuGet v3 flat container, npm registry API) — mostly
base URL + auth. Exception: pip on Azure Artifacts is PEP 503 simple-index (HTML), not
PyPI's JSON API — a new parse path; build only if the client needs pip.

**Design decisions (settled):**
- Config: `registries.yml` (or env equivalent) mapping ecosystem → `{source: public |
  azure-feed, url, auth}`; a bare feed name is insufficient (Azure needs org + project +
  feed, per-ecosystem endpoints differ). Optionally overridable per repo.
- Auth: PAT basic auth locally → Entra ID token at client (same pattern as the GitHub
  swap in MIGRATION.md).
- Feed = source of truth for verdicts/actions. Optional upstream comparison surfaces as
  an informational digest line ("newer upstream, not yet in feed — flag to platform
  team") — turns the limitation into a curation signal.
- Verdict cache key MUST gain the registry-source identity: two repos on different feeds
  can have different "latest" for the same package; today's key would leak verdicts
  across feeds.
- Don't assume feed ≈ public: upstream-proxying feeds mirror latest automatically, but
  curated feeds lag deliberately.

**Scope for first cut:**
- [ ] `registries.yml` config + loader (fail fast on bad mapping)
- [ ] NuGet + npm Azure Artifacts feed lookups (same protocol, feed base URL + auth)
- [ ] Cache key includes registry source
- [ ] Digest line for "newer upstream, not in feed"
- [ ] Hold pip simple-index parsing until a client repo needs it

**When:** before client migration — pairs with the Phase 0 question "which ecosystems do
the 30 repos actually use"; add "and from which feeds?" to that question.

---

## v2 / v3 roadmap (for the design doc, not for building now)

- **v2:** Actuator — auto-comment `@dependabot recreate` on SUPERSEDED + close DUPLICATEs, gated per-repo, enabled only after audit log shows verdict precision over 4–6 weeks
- **v3:** Supervising agent per PR with human approval gates (AEGIS pattern) — only if v2 data justifies it
- **Complementary (recommend to team now):** Dependabot `groups` config in `dependabot.yml` to cut PR volume at the source; this system then handles staleness, which grouping doesn't solve

---

## Definition of done (v1)

- [ ] Runs end-to-end against test repos with one command
- [ ] All parser variants covered by fixture tests
- [ ] Zero-AI mode works (deterministic-only report)
- [ ] Verdict cache demonstrably deduplicates cross-repo calls
- [ ] Every verdict in audit log with model + prompt version + token count
- [ ] NEEDS_HUMAN rate visible per run
- [ ] MIGRATION.md complete — client swap is config/adapters only
- [ ] Cost per run logged (expect well under $1/week at client scale)
