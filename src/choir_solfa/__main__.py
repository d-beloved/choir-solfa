"""`python -m choir_solfa`: the same as the `choir-solfa` command."""
import sys

from .cli import main

sys.exit(main())
