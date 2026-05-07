"""`python3 -m deile` entry point (delegates to deile.cli)."""

import sys

from deile.cli import main

if __name__ == "__main__":
    sys.exit(main())
