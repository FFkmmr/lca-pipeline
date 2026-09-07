#!/usr/bin/env python3
"""Entry point: `python run.py [csv] [--db PATH] [--append] [-v]`."""

import sys

from src.main import main

if __name__ == "__main__":
    sys.exit(main())
