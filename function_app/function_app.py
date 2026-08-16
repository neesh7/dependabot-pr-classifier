"""Azure Functions thin wrapper — Timer trigger calling src.main.run().

Deploy the repo root as the function app (src/ must be on the path).
Test locally: `func start` with Azure Functions Core Tools.
"""

import logging
import sys
from pathlib import Path

import azure.functions as func

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

app = func.FunctionApp()


@app.timer_trigger(schedule="0 0 8 * * 1", arg_name="timer",
                   run_on_startup=False, use_monitor=True)  # Mondays 08:00 UTC
def weekly_triage(timer: func.TimerRequest) -> None:
    from src.main import run

    digest = run()
    logging.info("Dependabot triage digest generated (%d chars)", len(digest))
