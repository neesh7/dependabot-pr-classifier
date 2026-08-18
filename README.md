# dependabot-pr-classifier

AI-powered triage for Dependabot PRs — detects stale, superseded, and duplicate dependency PRs across repos and recommends actions using GPT models on Azure OpenAI (Microsoft Foundry).

Report-only (v1): never writes to any PR. Deterministic checks run first; the model only judges what logic can't resolve, with read-only tools and a cross-repo verdict cache.

Microsoft-native: Azure OpenAI for inference, Entra ID (managed identity) for auth, Azure Functions for scheduling.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in GITHUB_TOKEN, REPOS, FOUNDRY_ENDPOINT
az login               # or set FOUNDRY_API_KEY for key-based auth
```

## Usage

```bash
python scripts/smoke_test.py     # pre-flight: endpoint, auth, deployment names
python main.py                  # full pipeline -> digest to stdout + data/digest.md
python main.py --no-ai          # deterministic only, zero LLM calls
python main.py --collect-only   # raw PR records as JSON
python main.py --grouped        # PRs grouped by (repo, ecosystem, manifest, dependency)
pytest                          # 60 tests, no network/API needed
```

Outputs: `data/digest.md` (weekly digest), `data/audit_log.jsonl` (append-only verdict log), `data/verdict_cache.json` (cross-repo AI cache).

## Docs

- `PLAN.md` — build plan and architecture
- `MIGRATION.md` — swap list for the client Azure env (Foundry, Entra ID, Table Storage, Functions)
- `CLAUDE.md` — working notes for agents: pipeline map and invariants
