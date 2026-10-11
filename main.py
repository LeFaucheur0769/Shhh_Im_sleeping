#!/usr/bin/env python3
"""Shhh, I'm sleeping -- entry point.

The real code lives in the shhh_im_sleeping package; this thin wrapper keeps
"python3 main.py" working.
"""

import sys

from shhh_im_sleeping.cli import main

if __name__ == "__main__":
    sys.exit(main())
