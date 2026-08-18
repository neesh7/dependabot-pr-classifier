# Migration: local build env → client Azure env

Zero logic changes. Every swap below is config or an adapter behind an existing
boundary. The only module that touches an LLM SDK is `src/llm/llm_client.py`.

**This branch is Microsoft-native**: inference is GPT on Azure OpenAI (Foundry
resource) via the `openai` SDK. The Anthropic path has been removed entirely, so
the "swap providers at migration" step no longer exists — it is already done.

## Swap list

| Local (build env)              | Client env                                            | Where to change |
|--------------------------------|-------------------------------------------------------|-----------------|
| —                              | Azure OpenAI on a Foundry resource                    | Done: set `FOUNDRY_ENDPOINT`; client calls `<resource>.services.ai.azure.com/openai/v1/` |
| API key in `.env`              | Entra ID managed identity / Key Vault                 | Done: leave `FOUNDRY_API_KEY` blank → `DefaultAzureCredential` bearer token per request. Assign the Function App's managed identity **Cognitive Services User** on the Foundry resource |
| GitHub PAT (classic, `repo`)   | GitHub App, org-installed (PRs: read, contents: read) | `github_client.py` auth header; App token minting via installation token |
| `python -m src.main` / cron    | Azure Function, Timer trigger (weekly)                | `function_app/` already wraps `src.main.run()` — deploy as-is |
| Local JSON audit log + cache   | Azure Table Storage                                   | `storage/audit_log.py` + `storage/verdict_cache.py`: swap file I/O for Table SDK, same interfaces |
| Markdown file output           | Teams incoming webhook                                | already wired: set `TEAMS_WEBHOOK_URL` |
| Console JSON logs (`log.py`)   | Application Insights                                  | Functions runtime forwards stderr/logging automatically |

## Authentication

Two mutually exclusive modes, chosen by whether `FOUNDRY_API_KEY` is set:

| `FOUNDRY_API_KEY` | Auth | Use |
|---|---|---|
| set | resource API key | local testing |
| blank | Entra ID via `DefaultAzureCredential` | client env (managed identity), and `az login` locally |

Keyless requires `azure-identity` (in `requirements.txt`); the import is lazy, so
key-based runs never need it. The token provider is called on every request and the
credential handles caching/refresh. Scope defaults to
`https://cognitiveservices.azure.com/.default` — override with `FOUNDRY_TOKEN_SCOPE`
only for sovereign clouds.

## Model routing

`LLM_MODEL_DEFAULT` (routine verdicts) and `LLM_MODEL_ESCALATION` (major bumps /
grouped PRs / escalation) are **Azure deployment names**, not catalogue model IDs.
Both may point at the same deployment; escalation then no-ops.

Deployments differ in which sampling params they accept — newer reasoning models
reject `temperature`, and `max_tokens` vs `max_completion_tokens` varies by
generation. `llm_client.py` detects the rejection, drops or renames the param, and
remembers it per deployment, so no config change is needed.

## Pre-migration checks (from the design doc)

- [ ] Verify the client's Azure region supports Global Standard deployment for the
      chosen GPT models BEFORE committing in the design doc
- [ ] Confirm the deployment names in `LLM_MODEL_DEFAULT` / `LLM_MODEL_ESCALATION`
      exist in the target resource — Azure 404s on an unknown deployment
- [ ] Confirm which ecosystems the ~30 client repos use; add registry clients in
      `registry_client.py` if beyond pip/npm/NuGet (one method each)
- [ ] GitHub App created + installed org-wide with read-only PR/contents permissions

## Client-doc callouts

- Azure OpenAI applies content filtering by default; changelog analysis is benign,
  but a filtered response returns no usage block (the client tolerates this).
- Billing: Azure OpenAI is billed per-token on the Azure invoice under the
  resource's pricing; counts toward MACC for eligible customers.
- v1 is report-only: no writes to any PR. The audit log
  (`data/audit_log.jsonl` → Table Storage) accumulates the verdict-precision
  evidence needed to justify v2 automation.
