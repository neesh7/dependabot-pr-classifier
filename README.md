# dependabot-pr-classifier

AI-powered triage for Dependabot PRs — detects stale, superseded, and duplicate dependency PRs across repos and recommends actions using Claude.

Report-only (v1): never writes to any PR. Deterministic checks run first; Claude only judges what logic can't resolve, with read-only tools and a cross-repo verdict cache.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in GITHUB_TOKEN, ANTHROPIC_API_KEY, REPOS
```

## Usage

```bash
python main.py                  # full pipeline -> digest to stdout + data/digest.md
python main.py --no-ai          # deterministic only, zero Claude calls
python main.py --collect-only   # raw PR records as JSON
python main.py --grouped        # PRs grouped by (repo, ecosystem, manifest, dependency)
pytest                          # 50 tests, no network/API needed
```

Outputs: `data/digest.md` (weekly digest), `data/audit_log.jsonl` (append-only verdict log), `data/verdict_cache.json` (cross-repo AI cache).

## Docs

- `PLAN.md` — build plan and architecture
- `MIGRATION.md` — swap list for the client Azure env (Foundry, Entra ID, Table Storage, Functions)
