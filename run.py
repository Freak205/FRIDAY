#!/usr/bin/env python
"""FRIDAY entry point.

    python run.py                 interactive console
    python run.py say "..."       one-shot command
    python run.py serve           run the daemon
    python run.py skills          list skills
    python run.py audit           show recent actions
"""

from friday.cli import main

if __name__ == "__main__":
    main()
