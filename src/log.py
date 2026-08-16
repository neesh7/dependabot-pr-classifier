"""Structured JSON logging to stderr — maps 1:1 to Application Insights at migration."""

import json
import sys
from datetime import datetime, timezone


def log_event(event: str, **fields) -> None:
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                      "event": event, **fields}), file=sys.stderr)
