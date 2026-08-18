"""THE abstraction boundary for the LLM. Nothing else imports the openai SDK.

Azure OpenAI on a Foundry resource (https://<res>.services.ai.azure.com/openai/v1/),
authenticated by resource key or, when FOUNDRY_API_KEY is blank, Entra ID.

Model names here are Azure *deployment* names, not catalogue model IDs.
"""

import json
import re
import sys
from collections.abc import Callable

import openai
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.classifier.versions import is_major_gap
from src.config import Config
from src.llm.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_message
from src.llm.tools import TOOL_DEFINITIONS, ToolExecutor
from src.schemas import ClassifiedPR, Verdict, VerdictMeta

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
MAX_OUTPUT_TOKENS = 2048


def _foundry_resource_from(endpoint: str) -> str | None:
    """Resource name from any Foundry portal URL, e.g.
    https://my-res.services.ai.azure.com/api/projects/x -> my-res"""
    m = re.match(r"https?://([^./]+)\.services\.ai\.azure\.com", endpoint.strip())
    return m.group(1) if m else None


def _entra_token_provider(scope: str) -> Callable[[], str]:
    """Keyless auth: managed identity in Azure, az login / env vars locally.

    The SDK invokes this on every request, so DefaultAzureCredential handles the
    caching and refresh. Imported lazily — azure-identity is only needed keyless.
    """
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError:
        sys.exit("Keyless auth needs azure-identity (pip install azure-identity), "
                 "or set FOUNDRY_API_KEY")
    return get_bearer_token_provider(DefaultAzureCredential(), scope)


def _needs_human(reason: str) -> Verdict:
    return Verdict(verdict="NEEDS_HUMAN", recommended_action="manual review",
                   risk_of_newer_version="medium", reasoning=reason)


class LLMClient:
    def __init__(self, config: Config, executor: ToolExecutor | None = None):
        resource = config.foundry_resource or _foundry_resource_from(config.foundry_endpoint)
        if not resource:
            sys.exit("Set FOUNDRY_ENDPOINT (or FOUNDRY_RESOURCE) — expected a "
                     "https://<resource>.services.ai.azure.com/... URL")
        key = config.foundry_api_key or None
        self._client = openai.AzureOpenAI(
            base_url=f"https://{resource}.services.ai.azure.com/openai/v1/",
            api_version=config.azure_api_version,
            api_key=key,
            # mutually exclusive with api_key — pass exactly one
            azure_ad_token_provider=(None if key else
                                     _entra_token_provider(config.foundry_token_scope)),
        )
        self._config = config
        self._executor = executor or ToolExecutor(config.github_token)
        # deployments disagree on temperature / max_tokens; learned per model at runtime
        self._unsupported: dict[str, set[str]] = {}
        self.api_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    # ── public API ────────────────────────────────────────────────────────

    def analyze(self, cp: ClassifiedPR) -> tuple[Verdict, VerdictMeta]:
        model = self.choose_model(cp)
        verdict, meta = self._run(model, cp)
        # escalate a default-model NEEDS_HUMAN once, if a bigger deployment exists
        if (verdict.verdict == "NEEDS_HUMAN" and model == self._config.model_default
                and self._config.model_escalation != model and not self._over_budget()):
            verdict2, meta2 = self._run(self._config.model_escalation, cp)
            meta2.input_tokens += meta.input_tokens
            meta2.output_tokens += meta.output_tokens
            return verdict2, meta2
        return verdict, meta

    def choose_model(self, cp: ClassifiedPR) -> str:
        escalate = cp.record.is_grouped or any(
            a.to_ver and a.latest_ver and is_major_gap(a.to_ver, a.latest_ver)
            for a in cp.assessments)
        return self._config.model_escalation if escalate else self._config.model_default

    # ── internals ─────────────────────────────────────────────────────────

    def _over_budget(self) -> bool:
        return self.api_calls >= self._config.max_llm_calls_per_run

    def _tuning_kwargs(self, model: str) -> dict:
        """temperature + an output cap, minus whatever this deployment has rejected."""
        skip = self._unsupported.get(model, set())
        kwargs: dict = {}
        if "temperature" not in skip:
            kwargs["temperature"] = 0
        if "max_completion_tokens" not in skip:
            kwargs["max_completion_tokens"] = MAX_OUTPUT_TOKENS
        elif "max_tokens" not in skip:
            kwargs["max_tokens"] = MAX_OUTPUT_TOKENS
        return kwargs

    def _adapt(self, model: str, kwargs: dict, exc: openai.BadRequestError) -> bool:
        """Drop or rename one rejected param and retry; each is adapted once per model.

        Reasoning deployments reject temperature, and max_tokens vs
        max_completion_tokens depends on the model generation.
        """
        msg = str(exc)
        skip = self._unsupported.setdefault(model, set())
        for param, replacement in (("temperature", None),
                                   ("max_completion_tokens", "max_tokens"),
                                   ("max_tokens", "max_completion_tokens")):
            if param in msg and param in kwargs and param not in skip:
                skip.add(param)
                value = kwargs.pop(param)
                if replacement and replacement not in skip:
                    kwargs[replacement] = value
                return True
        return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30),
           retry=retry_if_exception_type(
               (openai.RateLimitError, openai.InternalServerError,
                openai.APIConnectionError)),
           reraise=True)
    def _create(self, model: str, messages: list, tool_choice: str | None = None):
        self.api_calls += 1
        kwargs = dict(model=model, messages=messages, tools=TOOL_DEFINITIONS,
                      **self._tuning_kwargs(model))
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        while True:
            try:
                resp = self._client.chat.completions.create(**kwargs)
                break
            except openai.BadRequestError as exc:
                if not self._adapt(model, kwargs, exc):
                    raise
        if resp.usage:  # absent on some filtered/error-shaped responses
            self.input_tokens += resp.usage.prompt_tokens
            self.output_tokens += resp.usage.completion_tokens
        return resp

    def _tool_results(self, message) -> list[dict]:
        results = []
        for call in message.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
                content = self._executor.execute(call.function.name, args)
            except json.JSONDecodeError as exc:
                content = f"Tool error: arguments were not valid JSON ({exc})"
            results.append({"role": "tool", "tool_call_id": call.id, "content": content})
        return results

    def _run(self, model: str, cp: ClassifiedPR) -> tuple[Verdict, VerdictMeta]:
        meta = VerdictMeta(model=model, prompt_version=PROMPT_VERSION)
        if self._over_budget():
            return _needs_human(
                f"LLM call cap ({self._config.max_llm_calls_per_run}) reached this run."), meta

        messages: list = [{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": build_user_message(cp)}]
        tokens_before = (self.input_tokens, self.output_tokens)

        for iteration in range(self._config.max_tool_iterations + 1):
            force_final = (iteration == self._config.max_tool_iterations
                           or self._over_budget())
            if force_final:
                messages.append({"role": "user", "content":
                                 "Tool budget exhausted. Give your final JSON verdict now."})
            resp = self._create(model, messages, tool_choice="none" if force_final else None)
            meta.tool_iterations = iteration
            message = resp.choices[0].message
            if not message.tool_calls:
                break
            messages.append(message.model_dump(exclude_none=True))
            messages.extend(self._tool_results(message))

        verdict = self._parse_verdict(model, messages, message.content or "")
        meta.input_tokens = self.input_tokens - tokens_before[0]
        meta.output_tokens = self.output_tokens - tokens_before[1]
        return verdict, meta

    def _parse_verdict(self, model: str, messages: list, text: str) -> Verdict:
        """Validate the JSON answer; on failure, ONE retry with the error fed back."""
        for attempt in range(2):
            m = _JSON_RE.search(text)
            try:
                return Verdict.model_validate_json(m.group(0) if m else text)
            except Exception as exc:
                if attempt == 1 or self._over_budget():
                    return _needs_human(f"Model output failed schema validation: {exc}")
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content":
                                 f"Your response failed validation: {exc}. "
                                 "Reply with ONLY the corrected JSON object."})
                resp = self._create(model, messages, tool_choice="none")
                text = resp.choices[0].message.content or ""
        return _needs_human("unreachable")


def extract_json(text: str) -> str | None:
    """Exposed for tests: first JSON object in a text blob."""
    m = _JSON_RE.search(text)
    return m.group(0) if m else None
