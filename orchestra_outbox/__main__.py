"""``python -m orchestra_outbox run``: the outbox machine, which ``orchestra run`` keeps running as
``outbox.sender``."""

import sys

from .machine import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
