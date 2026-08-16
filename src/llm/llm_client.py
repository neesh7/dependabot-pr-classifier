"""THE abstraction boundary for Claude. Nothing else imports the anthropic SDK.

Provider switch: LLM_PROVIDER=anthropic (local) | foundry (client env — same
Messages API via Foundry base_url + Entra ID token; filled in at migration).
"""

import json
import re
import sys

import anthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.classifier.versions import is_major_gap
from src.config import Config
from src.llm.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_message
from src.llm.tools import TOOL_DEFINITIONS, ToolExecutor
from src.schemas import ClassifiedPR, Verdict, VerdictMeta

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _needs_human(reason: str) -> Verdict:
    return Verdict(verdict="NEEDS_HUMAN", recommended_action="manual review",
                   risk_of_newer_version="medium", reasoning=reason)


class LLMClient:
    def __init__(self, config: Config, executor: ToolExecutor | None = None):
        if config.llm_provider == "anthropic":
            if not config.anthropic_api_key:
                sys.exit("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
            self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        elif config.llm_provider == "foundry":
            raise NotImplementedError(
                "Foundry provider is wired at client migration: same Messages API, "
                "Foundry base_url + Entra ID token (see MIGRATION.md)")
        else:
            sys.exit(f"Unknown LLM_PROVIDER: {config.llm_provider}")
        self._config = config
        self._executor = executor or ToolExecutor(config.github_token)
        self._no_temperature: set[str] = set()  # Claude 5 models reject the param
        self.api_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    # ── public API ────────────────────────────────────────────────────────

    def analyze(self, cp: ClassifiedPR) -> tuple[Verdict, VerdictMeta]:
        model = self.choose_model(cp)
        verdict, meta = self._run(model, cp)
        # escalate a default-model NEEDS_HUMAN once, if budget remains
        if (verdict.verdict == "NEEDS_HUMAN" and model == self._config.model_default
                and not self._over_budget()):
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30),
           retry=retry_if_exception_type(
               (anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.APIConnectionError)),
           reraise=True)
    def _create(self, model: str, messages: list, tool_choice: dict | None = None):
        self.api_calls += 1
        kwargs = dict(model=model, max_tokens=2048, system=SYSTEM_PROMPT,
                      tools=TOOL_DEFINITIONS, messages=messages)
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if model not in self._no_temperature:
            kwargs["temperature"] = 0
        try:
            resp = self._client.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            if "temperature" not in str(exc) or model in self._no_temperature:
                raise
            self._no_temperature.add(model)
            kwargs.pop("temperature", None)
            resp = self._client.messages.create(**kwargs)
        self.input_tokens += resp.usage.input_tokens
        self.output_tokens += resp.usage.output_tokens
        return resp

    def _run(self, model: str, cp: ClassifiedPR) -> tuple[Verdict, VerdictMeta]:
        meta = VerdictMeta(model=model, prompt_version=PROMPT_VERSION)
        if self._over_budget():
            return _needs_human(
                f"LLM call cap ({self._config.max_llm_calls_per_run}) reached this run."), meta

        messages = [{"role": "user", "content": build_user_message(cp)}]
        tokens_before = (self.input_tokens, self.output_tokens)

        for iteration in range(self._config.max_tool_iterations + 1):
            force_final = (iteration == self._config.max_tool_iterations
                           or self._over_budget())
            if force_final:
                messages.append({"role": "user", "content":
                                 "Tool budget exhausted. Give your final JSON verdict now."})
            resp = self._create(model, messages,
                                tool_choice={"type": "none"} if force_final else None)
            meta.tool_iterations = iteration
            if resp.stop_reason != "tool_use":
                break
            messages.append({"role": "assistant", "content": resp.content})
            results = [{"type": "tool_result", "tool_use_id": block.id,
                        "content": self._executor.execute(block.name, block.input)}
                       for block in resp.content if block.type == "tool_use"]
            messages.append({"role": "user", "content": results})

        text = "".join(b.text for b in resp.content if b.type == "text")
        verdict = self._parse_verdict(model, messages, resp, text)
        meta.input_tokens = self.input_tokens - tokens_before[0]
        meta.output_tokens = self.output_tokens - tokens_before[1]
        return verdict, meta

    def _parse_verdict(self, model: str, messages: list, resp, text: str) -> Verdict:
        """Validate the JSON answer; on failure, ONE retry with the error fed back."""
        for attempt in range(2):
            m = _JSON_RE.search(text)
            try:
                return Verdict.model_validate_json(m.group(0) if m else text)
            except Exception as exc:
                if attempt == 1 or self._over_budget():
                    return _needs_human(f"Model output failed schema validation: {exc}")
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content":
                                 f"Your response failed validation: {exc}. "
                                 "Reply with ONLY the corrected JSON object."})
                resp = self._create(model, messages, tool_choice={"type": "none"})
                text = "".join(b.text for b in resp.content if b.type == "text")
        return _needs_human("unreachable")


def extract_json(text: str) -> str | None:
    """Exposed for tests: first JSON object in a text blob."""
    m = _JSON_RE.search(text)
    return m.group(0) if m else None
