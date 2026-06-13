"""Entry point for running the Flight Card Scanner with uvicorn.

Usage:
    .venv/bin/python -m flight_card_scanner

Reads host/port from config.json (or CONFIG_PATH env var) and starts
uvicorn bound to that address. Defaults to 0.0.0.0:8000 (all interfaces).
"""

import os
from pathlib import Path

import uvicorn

from .config import load_config

config_path = Path(os.environ.get("CONFIG_PATH", "config.json"))
config = load_config(config_path)

if __name__ == "__main__":
    uvicorn.run(
        "flight_card_scanner.main:app",
        host=config.host,
        port=config.port,
        log_level="info",
    )
