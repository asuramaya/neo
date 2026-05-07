#!/usr/bin/env python3
"""
neo — see what Claude Code hides from you

  python3 neo.py              # ingest + setup + dashboard
  python3 neo.py --setup      # install hooks only
  python3 neo.py --ingest     # ingest data only
  python3 neo.py --dashboard  # dashboard only
  python3 neo.py --port 8888  # custom port
  python3 neo.py --dashboard --no-open  # don't auto-launch browser
"""

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from neo.app import main


if __name__ == "__main__":
    main()
