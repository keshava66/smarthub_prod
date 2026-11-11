"""
Loads backend/.env into os.environ.

A plain Python process doesn't read .env on its own (that's a Vite/node thing),
so importing this module does it. Uses python-dotenv if it's installed; otherwise
falls back to a tiny parser so you don't need the extra package.

Import it before anything that reads AZURE_OPENAI_* from the environment.
"""

import os
from pathlib import Path

ENV_FILE = Path(__file__).parent.parent / ".env"   # backend/.env


def _manual_load(path: Path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # don't clobber anything already set in the real environment
        os.environ.setdefault(key, val)


if ENV_FILE.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except ImportError:
        _manual_load(ENV_FILE)