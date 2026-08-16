"""Convenience entrypoint: `python main.py` == `python -m src.main --collect-only`."""

import sys

from src.main import main

if __name__ == "__main__":
    main(sys.argv[1:] or ["--collect-only"])
