#!/usr/bin/env python3
"""Shortcut: `python bmk.py <command>` == the chrome-bookmarks skill script."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / ".github" / "skills" / "chrome-bookmarks" / "scripts"))
from bookmarks import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
