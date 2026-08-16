"""Append-only verdict log (JSON lines now, Azure Table at migration).

One line per AI verdict. This is the evidence base for graduating to v2
automation ("AI said SUPERSEDED 84x, humans agreed 81x").
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from src.schemas import ClassifiedPR


class AuditLog:
    def __init__(self, path: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, cp: ClassifiedPR) -> None:
        if cp.verdict is None or cp.verdict_meta is None:
            return
        line = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "repo": cp.record.repo,
            "pr": cp.record.pr_number,
            "deps": [{"dependency": a.dependency, "from": a.from_ver,
                      "to": a.to_ver, "latest": a.latest_ver}
                     for a in cp.assessments],
            "verdict": cp.verdict.verdict,
            "recommended_action": cp.verdict.recommended_action,
            "model": cp.verdict_meta.model,
            "prompt_version": cp.verdict_meta.prompt_version,
            "input_tokens": cp.verdict_meta.input_tokens,
            "output_tokens": cp.verdict_meta.output_tokens,
            "cache_hit": cp.verdict_meta.cache_hit,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
