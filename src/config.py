"""All env-driven settings in one place. Fail fast on missing required vars."""

import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    github_token: str
    repos: list[str]
    llm_provider: str = "anthropic"
    anthropic_api_key: str = ""
    foundry_resource: str = ""  # Microsoft Foundry resource name (or set foundry_endpoint)
    foundry_endpoint: str = ""  # any portal endpoint URL; resource host is derived
    foundry_api_key: str = ""   # blank in client env -> Entra ID
    model_default: str = "claude-haiku-4-5-20251001"
    model_escalation: str = "claude-sonnet-5"
    max_llm_calls_per_run: int = 100
    max_tool_iterations: int = 5
    audit_log_path: str = "data/audit_log.jsonl"
    verdict_cache_path: str = "data/verdict_cache.json"
    digest_output_path: str = "data/digest.md"
    teams_webhook_url: str = ""


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("GITHUB_TOKEN", "")
    repos = [r.strip() for r in os.getenv("REPOS", "").split(",") if r.strip()]

    missing = [name for name, val in [("GITHUB_TOKEN", token), ("REPOS", repos)] if not val]
    if missing:
        sys.exit(f"Missing required env vars: {', '.join(missing)} (see .env.example)")

    return Config(
        github_token=token,
        repos=repos,
        llm_provider=os.getenv("LLM_PROVIDER", "anthropic"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        foundry_resource=os.getenv("FOUNDRY_RESOURCE", ""),
        foundry_endpoint=os.getenv("FOUNDRY_ENDPOINT", ""),
        foundry_api_key=os.getenv("FOUNDRY_API_KEY", ""),
        model_default=os.getenv("LLM_MODEL_DEFAULT", Config.model_default),
        model_escalation=os.getenv("LLM_MODEL_ESCALATION", Config.model_escalation),
        max_llm_calls_per_run=int(os.getenv("LLM_MAX_CALLS_PER_RUN", "100")),
        max_tool_iterations=int(os.getenv("LLM_MAX_TOOL_ITERATIONS", "5")),
        audit_log_path=os.getenv("AUDIT_LOG_PATH", Config.audit_log_path),
        verdict_cache_path=os.getenv("VERDICT_CACHE_PATH", Config.verdict_cache_path),
        digest_output_path=os.getenv("DIGEST_OUTPUT_PATH", Config.digest_output_path),
        teams_webhook_url=os.getenv("TEAMS_WEBHOOK_URL", ""),
    )
