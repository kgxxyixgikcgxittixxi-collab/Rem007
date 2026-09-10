#!/usr/bin/env python3
"""rem_247 — Chạy Rem Agent 24/7 với multi-key architecture.

Usage:
    python3 rem_247.py              # Chạy liên tục
    python3 rem_247.py --once       # Chạy 1 cycle rồi thoát
    python3 rem_247.py --status     # Xem trạng thái
"""

import sys, os, time, signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import groq
from arch import orchestrator, init_arch, get_pool_stats, Watchdog
from social_auto import run_social_cycle, get_daily_report


def status():
    print("=== Rem Agent 24/7 Status ===")
    print(f"Version: {config.VERSION}")
    print(f"Keys available: {len(groq.keys())}")
    print(f"\nKey Pool Stats:")
    print(get_pool_stats())
    print(f"\nSocial Auto:")
    print(f"  Enabled: {config.SOCIAL_AUTO['enabled']}")
    print(f"  Platforms: {', '.join(config.SOCIAL_AUTO['platforms'])}")
    print(f"  Daily max posts: {config.SOCIAL_AUTO['max_daily_posts']}")
    print(f"\nSocial DB:")
    print(get_daily_report())
    print("\n24/7 Settings:")
    print(f"  Auto resume: {config.OP_247['auto_resume']}")
    print(f"  Auto restart: {config.OP_247['auto_restart']}")
    print(f"  Quiet hours: {config.OP_247['quiet_hours']}")


def run_once():
    """Chạy 1 cycle."""
    print("[24/7] Running 1 cycle...")

    # Test main pool
    msgs = [{"role": "user", "content": "Xin chào, test 1 lần"}]
    result = groq.chat(msgs, budget=30)
    if result:
        print("[main] ✓ Response received")
    else:
        print("[main] ✗ No response")

    # Test social auto
    result = run_social_cycle()
    print("[social] ✓ Cycle complete")
    print(result)


def run_forever():
    """Chạy liên tục 24/7."""
    print(f"[24/7] Rem Agent v{config.VERSION} starting...")
    print(f"[24/7] Keys: {len(groq.keys())}")
    print(f"[24/7] Social: {config.SOCIAL_AUTO['enabled']}")
    print(f"[24/7] Quiet hours: {config.OP_247['quiet_hours']}")

    # Initialize watchdog
    init_arch()

    # Handle signals
    def shutdown(sig, frame):
        print("\n[24/7] Shutting down...")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    interval = config.SOCIAL_AUTO["interval_minutes"] * 60
    quiet_start, quiet_end = config.OP_247["quiet_hours"]

    while True:
        now = time.localtime()
        hour = now.tm_hour

        # Quiet hours check
        if quiet_start <= hour < quiet_end:
            time.sleep(300)  # 5 min check
            continue

        try:
            # Run social cycle
            result = run_social_cycle()
            print(f"[24/7 {time.strftime('%H:%M')}] {result}")
        except Exception as e:
            print(f"[24/7] ERROR: {e}")
            if config.OP_247["auto_restart"]:
                print("[24/7] Restarting...")
                time.sleep(5)
                continue

        # Wait for next cycle
        time.sleep(interval)


if __name__ == "__main__":
    if "--status" in sys.argv:
        status()
    elif "--once" in sys.argv:
        run_once()
    else:
        run_forever()