# Migration: local build env → client Azure env

Zero logic changes. Every swap below is config or an adapter behind an existing
boundary. The only module that touches the Anthropic SDK is `src/llm/llm_client.py`.

## Swap list

| Local (build env)              | Client env                                            | Where to change |
|--------------------------------|-------------------------------------------------------|-----------------|
| Anthropic API + API key        | Claude in Microsoft Foundry (Azure-hosted, GA)        | Done: set `LLM_PROVIDER=foundry` + `FOUNDRY_ENDPOINT`. Same Messages API via `AnthropicFoundry` |
| API key in `.env`              | Entra ID managed identity / Key Vault                 | Done: leave `FOUNDRY_API_KEY` blank → `DefaultAzureCredential` bearer token per request. Assign the Function App's managed identity **Cognitive Services User** on the Foundry resource |
| GitHub PAT (classic, `repo`)   | GitHub App, org-installed (PRs: read, contents: read) | `github_client.py` auth header; App token minting via installation token |
| `python -m src.main` / cron    | Azure Function, Timer trigger (weekly)                | `function_app/` already wraps `src.main.run()` — deploy as-is |
| Local JSON audit log + cache   | Azure Table Storage                                   | `storage/audit_log.py` + `storage/verdict_cache.py`: swap file I/O for Table SDK, same interfaces |
| Markdown file output           | Teams incoming webhook                                | already wired: set `TEAMS_WEBHOOK_URL` |
| Console JSON logs (`log.py`)   | Application Insights                                  | Functions runtime forwards stderr/logging automatically |

## Foundry authentication

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

Haiku (routine verdicts) and Sonnet (major bumps / grouped / escalation) are both
available in Foundry, so `LLM_MODEL_DEFAULT` / `LLM_MODEL_ESCALATION` just change
to the Foundry deployment names. Note: Claude 5-family models reject the
`temperature` parameter; `llm_client.py` handles this automatically.

## Pre-migration checks (from the design doc)

- [ ] Verify the client's Azure region supports Global Standard deployment for the
      chosen Claude models BEFORE committing in the design doc
- [ ] Confirm which ecosystems the ~30 client repos use; add registry clients in
      `registry_client.py` if beyond pip/npm/NuGet (one method each)
- [ ] GitHub App created + installed org-wide with read-only PR/contents permissions

## Client-doc callouts

- Foundry has no built-in content filtering for Claude — Azure AI Content Safety is
  optional (low relevance for changelog analysis); Anthropic AUP compliance applies.
- Billing: Claude in Foundry bills via CCUs on the Azure invoice; counts toward
  MACC for eligible customers.
- v1 is report-only: no writes to any PR. The audit log
  (`data/audit_log.jsonl` → Table Storage) accumulates the verdict-precision
  evidence needed to justify v2 automation.
