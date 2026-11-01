"""Allow ``python -m invengo`` to run the CLI."""
import sys

from .cli import main

sys.exit(main())
