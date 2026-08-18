"""Pre-flight check: endpoint, auth, and deployment name — one cheap call each.

Run before the full pipeline so a misconfigured resource fails in seconds instead
of mid-run. Needs no GitHub token and touches no repos.

    python scripts/smoke_test.py                  # checks the configured deployments
    python scripts/smoke_test.py -d my-gpt-dep    # checks one specific deployment
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import replace  # noqa: E402

from src.config import Config, load_config  # noqa: E402
from src.llm.llm_client import LLMClient, _foundry_resource_from  # noqa: E402

OK, BAD = "PASS", "FAIL"


def _diagnose(exc: Exception) -> str:
    """Map the usual Azure failures onto the thing you actually have to fix."""
    import openai
    text = str(exc)
    if "PermissionDenied" in text or isinstance(exc, openai.PermissionDeniedError):
        return ("authenticated, but this identity has no data-plane access — assign "
                "Cognitive Services OpenAI User on the resource. Subscription Owner is "
                "a control-plane role and does NOT grant it")
    if isinstance(exc, openai.NotFoundError) or "DeploymentNotFound" in text:
        return ("deployment name not found on this resource — check the exact name under "
                "Models + endpoints in the Foundry portal (it is the deployment name, "
                "not the catalogue model id)")
    if isinstance(exc, openai.AuthenticationError) or " 401" in text:
        return ("auth rejected — with a key: copy a current one from Keys and Endpoint; "
                "keyless: run az login and check the role assignment has propagated")

    if isinstance(exc, openai.RateLimitError):
        return "rate limited or out of quota — raise the deployment TPM quota"
    if isinstance(exc, openai.APIConnectionError):
        return "could not reach the endpoint — check FOUNDRY_ENDPOINT and network/firewall"
    return text[:300]


def check(config: Config, deployment: str) -> bool:
    llm = LLMClient(config, executor=object())  # executor unused: no tools are invoked
    try:
        resp = llm._create(deployment, [{"role": "user",
                                         "content": "Reply with the single word: ok"}])
    except Exception as exc:
        print(f"  {BAD} {deployment}: {_diagnose(exc)}")
        return False

    reply = (resp.choices[0].message.content or "").strip()[:40]
    dropped = llm._unsupported.get(deployment, set())
    note = f" (adapted params: {', '.join(sorted(dropped))})" if dropped else ""
    print(f"  {OK} {deployment}: replied {reply!r}, "
          f"{llm.input_tokens + llm.output_tokens} tokens{note}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-d", "--deployment", action="append",
                        help="deployment name to test (repeatable); "
                             "defaults to LLM_MODEL_DEFAULT and LLM_MODEL_ESCALATION")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config = load_config()  # same .env, same validation as the pipeline
    resource = config.foundry_resource or _foundry_resource_from(config.foundry_endpoint)
    if not resource:
        print(f"{BAD} no resource: set FOUNDRY_ENDPOINT to a "
              "https://<resource>.services.ai.azure.com/... URL, or FOUNDRY_RESOURCE")
        return 1
    config = replace(config, foundry_resource=resource)
    key = config.foundry_api_key

    print(f"Resource:   {resource}")
    print(f"Base URL:   https://{resource}.services.ai.azure.com/openai/v1")
    print(f"Auth:       {'resource key' if key else 'Entra ID (DefaultAzureCredential)'}")
    if not key:
        print(f"Scope:      {config.foundry_token_scope}")

    deployments = args.deployment or list(dict.fromkeys(
        [config.model_default, config.model_escalation]))
    print(f"Deployments: {', '.join(deployments)}\n")

    ok = all([check(config, d) for d in deployments])  # list: check every one
    print("\nReady to run the pipeline." if ok else
          "\nFix the failures above before running the pipeline.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
