import os
import sys

# Allow `python -m pytest tests/` from backend/ as well as from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))