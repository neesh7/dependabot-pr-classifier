"""Cross-repo AI verdict cache (JSON file now, Azure Table at migration).

Key: (ecosystem, dep, to_ver, latest_ver) per dependency + prompt version —
package-level facts don't depend on the repo, so 14 repos with the same
axios bump cost 1 Claude call.
"""

import json
from pathlib import Path

from src.llm.prompts import PROMPT_VERSION
from src.schemas import ClassifiedPR, Verdict, VerdictMeta


class VerdictCache:
    def __init__(self, path: str):
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    @staticmethod
    def key_for(cp: ClassifiedPR) -> str:
        parts = sorted(
            f"{cp.record.ecosystem}|{a.dependency}|{a.to_ver}|{a.latest_ver}"
            for a in cp.assessments)
        return "&".join(parts) + f"@{PROMPT_VERSION}"

    def get(self, key: str) -> tuple[Verdict, VerdictMeta] | None:
        entry = self._data.get(key)
        if not entry:
            return None
        meta = VerdictMeta.model_validate(entry["meta"])
        meta.cache_hit = True
        meta.input_tokens = meta.output_tokens = meta.tool_iterations = 0
        return Verdict.model_validate(entry["verdict"]), meta

    def put(self, key: str, verdict: Verdict, meta: VerdictMeta) -> None:
        self._data[key] = {"verdict": verdict.model_dump(), "meta": meta.model_dump()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
