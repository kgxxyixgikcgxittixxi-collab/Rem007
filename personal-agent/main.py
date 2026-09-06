#!/usr/bin/env python3
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from providers import groq
from extensions import Manager
from repl import Repl


def main():
    ks = groq.keys()
    print(f"[{config.NAME} v{config.VERSION}] Groq keys: {len(ks)} | DB: {config.DIR}")
    if not ks:
        print("Chưa có Groq key nào. Lấy key tại console.groq.com rồi gõ /key gsk_...")
    else:
        print(f"[models] chat: {groq.chat_models()[0] if groq.chat_models() else '?'}")
        print(f"[models] compact: {groq.clone_models()[0] if groq.clone_models() else '?'}")
    manager = Manager()
    repl = Repl(manager)
    try:
        repl.run()
    finally:
        manager.close_all()


if __name__ == "__main__":
    main()