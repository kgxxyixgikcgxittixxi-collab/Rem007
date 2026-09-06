#!/usr/bin/env python3
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from extensions import Manager
from repl import Repl


def main():
    manager = Manager()
    repl = Repl(manager)
    try:
        repl.run()
    finally:
        manager.close_all()


if __name__ == "__main__":
    main()