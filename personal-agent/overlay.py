#!/usr/bin/env python3
"""Cửa sổ nổi Remtm: hiện việc đang làm (QUAN SÁT / PHÁT LẠI NHANH / XONG).
Chạy:  python3 overlay.py            (hiện cửa sổ, đọc ~/.rem_ai/overlay.json)
Dừng:  /overlay off  trong Remtm  (hoặc pkill -f overlay.py)
Không có màn hình (headless/ssh) → tự thoát, không crash agent.
"""
import json
import os
import sys
import time

HOME = os.path.expanduser("~")
DIR = os.path.join(HOME, ".rem_ai")
OVERLAY_FILE = os.path.join(DIR, "overlay.json")
PID_FILE = os.path.join(DIR, "overlay.pid")

MODE_COLOR = {
    "QUAN SÁT": "#f5b301",
    "PHÁT LẠI NHANH": "#35d07f",
    "XONG": "#4aa8ff",
    "RẢNH": "#8a8f98",
}


def _read():
    try:
        with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def main():
    if not os.environ.get("DISPLAY"):
        print("[overlay] không có màn hình (DISPLAY trống) — bỏ qua cửa sổ nổi.")
        return 0
    try:
        import tkinter as tk
    except Exception as e:
        print(f"[overlay] thiếu tkinter ({e}) — dùng: sudo apt install python3-tk")
        return 1
    os.makedirs(DIR, exist_ok=True)
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    root = tk.Tk()
    root.title("Remtm đang làm")
    root.geometry("380x210")
    try:
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.93)
    except Exception:
        pass
    root.configure(bg="#14161a")
    try:
        root.wm_attributes("-type", "dock")
    except Exception:
        pass

    mode_var = tk.StringVar(value="RẢNH")
    task_var = tk.StringVar(value="Chờ việc…")
    tool_var = tk.StringVar(value="")
    foot_var = tk.StringVar(value="")

    top = tk.Frame(root, bg="#14161a")
    top.pack(fill="x", padx=10, pady=(10, 2))
    dot = tk.Label(top, text="●", fg=MODE_COLOR["RẢNH"], bg="#14161a", font=("Sans", 14, "bold"))
    dot.pack(side="left")
    mode_lb = tk.Label(top, textvariable=mode_var, fg="white", bg="#14161a", font=("Sans", 11, "bold"))
    mode_lb.pack(side="left", padx=6)
    tk.Button(top, text="Ẩn", command=root.withdraw,
              bg="#23262c", fg="white", relief="flat").pack(side="right")

    task_lb = tk.Label(root, textvariable=task_var, fg="white", bg="#14161a",
                       font=("Sans", 10), wraplength=350, justify="left", anchor="w")
    task_lb.pack(fill="x", padx=10, pady=2)
    tool_lb = tk.Label(root, textvariable=tool_var, fg="#9fe8b8", bg="#14161a",
                       font=("Sans", 9), wraplength=350, justify="left", anchor="w")
    tool_lb.pack(fill="x", padx=10, pady=2)
    foot_lb = tk.Label(root, textvariable=foot_var, fg="#8a8f98", bg="#14161a", font=("Sans", 8))
    foot_lb.pack(fill="x", padx=10, pady=(2, 6))

    t0 = time.time()

    def tick():
        d = _read()
        mode = str(d.get("mode", "RẢNH") or "RẢNH").upper()
        if mode not in MODE_COLOR:
            mode = "RẢNH" if not d.get("task") else mode
        task = str(d.get("task", "Chờ việc…") or "Chờ việc…")[:220]
        tool = str(d.get("tool", "") or "")[:160]
        prog = str(d.get("progress", "") or "")[:120]
        upd = d.get("updated", 0) or 0
        el = int(time.time() - (upd or t0)) if d.get("task") else 0
        mode_var.set(mode)
        try:
            dot.configure(fg=MODE_COLOR.get(mode, "#8a8f98"))
        except Exception:
            pass
        task_var.set(task)
        tool_var.set(f"🔧 {tool}" if tool else "")
        foot_var.set(f"{prog}  ·  {el}s" if prog or el else "Remtm · /overlay off để tắt")
        root.after(500, tick)

    def on_close():
        try:
            if os.path.isfile(PID_FILE):
                os.remove(PID_FILE)
            with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
                json.dump({"mode": "RẢNH", "task": "", "tool": "",
                           "progress": "đã ẩn cửa sổ", "updated": time.time()},
                          f, ensure_ascii=False)
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    tick()
    try:
        root.mainloop()
    finally:
        try:
            if os.path.isfile(PID_FILE):
                os.remove(PID_FILE)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
