"""Convenience entrypoint: `python main.py` == `python -m src.main` (full pipeline)."""

import sys

from src.main import main

if __name__ == "__main__":
    main(sys.argv[1:])
