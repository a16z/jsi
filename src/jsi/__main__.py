"""main module for jsi.

Usage:
    python -m jsi [options] <path/to/query.smt2>
"""

import click
import sys

from jsi import main

sys.exit(main())
