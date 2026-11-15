"""Run Crow OS with `python -m crow_os` from an installed package or PYTHONPATH=src."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
