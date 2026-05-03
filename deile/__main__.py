"""`python3 -m deile` entry point (delegates to deile.cli)."""

from deile.cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
