"""Allow ``python -m ensp_mcp.cli``."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
