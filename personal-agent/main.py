#!/usr/bin/env python3
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import updater
from extensions import Manager
from repl import Repl


def main():
    updater.preflight()          # báo / tự cập nhật bản mới từ GitHub
    manager = Manager()
    # headless: Remtm h + "nội dung task" → chạy 1 task rồi tự thoát
    # resume:   Remtm c <session_id> + "nội dung" → TIẾP TỤC phiên cũ (giữ context, nghiên cứu đã có)
    headless = None
    resume_sid = None
    args = sys.argv[1:]
    if len(args) >= 3 and args[0] in ("c", "continue", "--continue"):
        headless = " ".join(args[2:]).strip()
        resume_sid = args[1]
    elif len(args) >= 2 and args[0] in ("h", "headless", "--task", "run"):
        headless = " ".join(args[1:]).strip()
    repl = Repl(manager, headless=headless, resume_sid=resume_sid)
    try:
        repl.run()
    finally:
        manager.close_all()
    if headless is not None:
        # exit code phản ánh kết quả (0 = xong, 1 = lỗi) để wrapper biết mà cảnh báo
        sys.exit(0 if (repl._last_out and not repl._last_out.strip().startswith("[")) else 1)


if __name__ == "__main__":
    main()
