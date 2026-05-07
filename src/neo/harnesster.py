#!/usr/bin/env python3
import sys
from .app import main as neo_main

def main(argv=None) -> None:
    forwarded_args = list(sys.argv[1:] if argv is None else argv)
    print(
        "[harnesster.py] this entry point has been renamed to neo.py. "
        "Please update your scripts. Forwarding for now.",
        file=sys.stderr,
    )
    neo_main(forwarded_args)


if __name__ == "__main__":
    main()
