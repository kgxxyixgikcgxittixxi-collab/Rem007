#!/usr/bin/env python3
"""Remtm fullscreen TUI kiểu opencode (Textual) — v1.

- KHÔNG đụng backend: dùng nguyên Agent/sessions/Manager/tools hiện có.
- REPL cũ (repl.py) giữ nguyên làm fallback: `Remtm` (cũ) vs `Remtm --tui` (mới).
- Bố cục opencode: header + chat bubbles markdown + thinking mờ thu gọn +
  timeline tool + input nhiều dòng + statusbar. Tab chuyển plan/build, ESC dừng.
"""
import os
import sys
import time
import queue
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from textual.app import App, ComposeResult
    from textual.widgets import TextArea, Static, Markdown, Collapsible
    from textual.containers import VerticalScroll
    from textual.binding import Binding
except Exception as _e:
    print(f"[Remtm] Thiếu textual ({_e}). Cài: pip install textual")
    sys.exit(2)

import config
import sessions
from agentloop import Agent
from permissions import Presets
from providers import groq

try:
    from repl import _TOOL_LABEL
except Exception:
    _TOOL_LABEL = {}


def _label(name, args):
    base = _TOOL_LABEL.get(name, name)
    note = ""
    if isinstance(args, dict):
        for k in ("command", "path", "query", "url", "pattern", "title"):
            v = args.get(k)
            if v not in (None, ""):
                note = str(v).strip().split("\n")[0][:64]
                break
    return f"{base} · {note}" if note else base


class ChatInput(TextArea):
    """Ô nhập nhiều dòng: Enter gửi, Shift+Enter xuống dòng (kiểu opencode)."""
    BINDINGS = [Binding("enter", "send", "Gửi", show=False)]

    def action_send(self):
        self.app.send_current()


class RemTUI(App):
    TITLE = f"REM v{config.VERSION} · opencode-style"
    CSS = """
    #head { height: 3; border-bottom: solid #444444; padding: 0 1; }
    #chat { height: 1fr; }
    #input { height: 6; border-top: solid #444444; }
    #status { height: 1; color: #888888; padding: 0 1; }
    .user { border-left: thick #00aa55; padding: 0 1; margin: 1 0; }
    .think { color: #888888; }
    .toolrow { color: #44dd88; }
    .toolerr { color: #ff5555; }
    .statustext { color: #00bbbb; }
    """

    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.sid = sessions.new()
        self.presets = "build"
        self.perm = Presets.build()
        self.agent = None
        self._mk_agent()
        self.busy = False
        self.evq = queue.Queue()
        self.show_thinking = True
        self.last_think = ""
        self._live_md = None
        self._live_buf = ""
        self._live_think_buf = ""
        self._live_last = 0.0
        self._tool_t0 = {}
        self._tool_rows = {}
        self._chat = None
        self._input = None
        self._status = None
        self._head = None

    def _mk_agent(self):
        self.agent = Agent(self.manager, self.perm, sid=self.sid, on_event=self._emit)
        try:
            self.agent.askfn = lambda n, a: True  # full-auto: TUI không hỏi giữa chừng
        except Exception:
            pass

    def _emit(self, ev):
        try:
            self.evq.put(ev)
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        yield Static(self._head_text(), id="head")
        with VerticalScroll(id="chat"):
            yield Static("Gõ câu hỏi · /help lệnh · Tab chuyển plan/build · ESC dừng", classes="think")
        yield ChatInput(id="input")
        yield Static(self._status_text(), id="status")

    def on_mount(self):
        self._chat = self.query_one("#chat")
        self._input = self.query_one("#input")
        self._status = self.query_one("#status")
        self._head = self.query_one("#head")
        self.set_interval(0.12, self._drain)
        self._input.focus()

    def _head_text(self):
        try:
            ms = groq.chat_models()
            model = ms[0] if ms else "chưa có key"
        except Exception:
            model = "?"
        try:
            nk = len(groq.keys())
        except Exception:
            nk = 0
        pill = "[plan]" if self.presets == "plan" else "[build]"
        return f"◆ REM v{config.VERSION} {pill}\nmodel {model} · {nk} keys\n{sessions.get_title(self.sid)[:60]}"

    def _status_text(self):
        pill = "[plan]" if self.presets == "plan" else "[build]"
        return f"↵ send · / commands · @ file · esc stop {pill} · {os.path.basename(os.getcwd())} · v{config.VERSION}"

    def _refresh_chrome(self):
        try:
            self._head.update(self._head_text())
        except Exception:
            pass
        try:
            self._status.update(self._status_text())
        except Exception:
            pass

    async def _add(self, widget):
        await self._chat.mount(widget)
        self._chat.scroll_end(animate=False)

    def send_current(self):
        if self.busy:
            return
        try:
            text = self._input.text.strip()
        except Exception:
            text = ""
        if not text:
            return
        try:
            self._input.clear()
        except Exception:
            pass
        if text.startswith("/"):
            self._slash(text)
            return
        self._chat.mount(Static(f"[bold green]User>[/] {text}", classes="user"))
        self._chat.scroll_end(animate=False)
        self.busy = True
        self._live_buf = ""
        self._live_think_buf = ""
        self._live_md = None
        threading.Thread(target=self._run_task, args=(text,), daemon=True).start()

    def _run_task(self, text):
        try:
            self.manager.reset_interrupt()
        except Exception:
            pass
        try:
            out = self.agent.run(text)
        except Exception as e:
            out = f"[LOI] {type(e).__name__}: {e}"
        self.evq.put({"type": "done", "text": out or ""})

    def _drain(self):
        if self._chat is None:
            return
        dirty = False
        while True:
            try:
                ev = self.evq.get_nowait()
            except queue.Empty:
                break
            t = ev.get("type")
            if t == "stream_delta":
                kind, s = ev.get("kind", "content"), ev.get("text", "")
                if kind == "thinking":
                    self._live_think_buf += s
                else:
                    self._live_buf += s
                now = time.time()
                if now - self._live_last > 0.4:
                    self._live_last = now
                    self._render_live()
            elif t == "tool_start":
                name = ev.get("name", "?")
                row = Static(f"⏳ {_label(name, ev.get('args'))} …", classes="statustext")
                self._chat.mount(row)
                self._tool_rows[name + str(time.time())] = row
                self._tool_t0[name] = time.time()
                self._chat.scroll_end(animate=False)
            elif t == "tool_done":
                name = ev.get("name", "?")
                res = str(ev.get("result") or "")
                ok = not res.startswith(("[LOI]", "[TOOL LOI]", "[TU CHOI]"))
                dt = time.time() - self._tool_t0.pop(name, time.time())
                self._chat.mount(Static(
                    f"{'✓' if ok else '✗'} {_label(name, {})} · {dt:.1f}s",
                    classes="toolrow" if ok else "toolerr"))
                self._chat.scroll_end(animate=False)
            elif t in ("thinking", "llm", "turn", "retry"):
                pass  # spinner trạng thái gọn — không spam dòng
            elif t == "done":
                self._finish(str(ev.get("text") or ""))
            dirty = True
        if dirty:
            self._refresh_chrome()

    def _render_live(self):
        if not self._live_buf.strip():
            return
        try:
            if self._live_md is None:
                self._live_md = Markdown(self._live_buf[:4000])
                self._chat.mount(self._live_md)
            else:
                self._live_md.update(self._live_buf[:8000])
            self._chat.scroll_end(animate=False)
        except Exception:
            pass

    def _finish(self, out):
        self.busy = False
        try:
            import render
            think, body = render.split_thinking(out)
        except Exception:
            think, body = "", out
        try:
            lr = (getattr(self.agent, "last_reasoning", "") or "").strip()
        except Exception:
            lr = ""
        if lr and lr not in think:
            think = (lr + ("\n" + think if think else "")).strip()[-6000:]
        self.last_think = think
        if self._live_md is not None:
            try:
                self._live_md.remove()
            except Exception:
                pass
            self._live_md = None
        if think.strip() and self.show_thinking:
            self._chat.mount(Collapsible(Markdown(think[:3000]), title="Đã suy luận", collapsed=True))
        if (body or "").strip():
            self._chat.mount(Markdown(body[:12000]))
        elif not think.strip():
            self._chat.mount(Static("(rỗng — gõ 'tiếp tục' để chạy tiếp)", classes="toolerr"))
        self._chat.scroll_end(animate=False)
        self._refresh_chrome()

    def _slash(self, line):
        cmd = line.strip().split()[0].lower()
        if cmd in ("/exit", "/q", "/quit"):
            self.exit()
        elif cmd == "/clear":
            try:
                for w in list(self._chat.children):
                    w.remove()
            except Exception:
                pass
        elif cmd == "/help":
            self._chat.mount(Markdown(
                "**Lệnh TUI:** /help /clear /exit · /plan /build (Tab) · /status /usage · "
                "/think (xem suy luận đầy đủ) /thinking (bật/tắt) · /stop · /model (xem model)\n"
                "Lệnh khác dùng REPL cũ: thoát rồi chạy `Remtm` (không --tui)."))
            self._chat.scroll_end(animate=False)
        elif cmd == "/plan":
            self.perm = Presets.plan()
            self.presets = "plan"
            self._mk_agent_keep_perm()
            self._note("Đã chuyển preset PLAN — chỉ đọc.")
        elif cmd == "/build":
            self.perm = Presets.build()
            self.presets = "build"
            self._mk_agent_keep_perm()
            self._note("Đã chuyển preset BUILD — full-auto.")
        elif cmd == "/status":
            self._note(self._head_text().replace("\n", " · "))
        elif cmd == "/usage":
            try:
                u = groq.session_usage()
                self._note(f"Token phiên: ↑{u.get('prompt', 0)} ↓{u.get('completion', 0)}")
            except Exception as e:
                self._note(f"[LOI] usage: {e}")
        elif cmd == "/model":
            try:
                self._note("Model: " + ", ".join(groq.chat_models()[:4]))
            except Exception as e:
                self._note(f"[LOI] model: {e}")
        elif cmd == "/think":
            self._chat.mount(Markdown(("**Suy luận đầy đủ:**\n" + self.last_think[:6000]) if self.last_think.strip() else "(chưa có)"))
            self._chat.scroll_end(animate=False)
        elif cmd == "/thinking":
            self.show_thinking = not self.show_thinking
            self._note(f"Hiện suy luận: {'BẬT' if self.show_thinking else 'TẮT'}")
        elif cmd == "/stop":
            self._stop_all()
        else:
            self._note(f"Lệnh {cmd} chưa có ở TUI — dùng REPL cũ (`Remtm`).")

    def _mk_agent_keep_perm(self):
        sid = self.sid
        self.agent = Agent(self.manager, self.perm, sid=sid, on_event=self._emit)
        try:
            self.agent.askfn = lambda n, a: True
        except Exception:
            pass
        self._refresh_chrome()

    def _note(self, s):
        self._chat.mount(Static(s, classes="think"))
        self._chat.scroll_end(animate=False)

    def _stop_all(self):
        try:
            self.agent.stop()
        except Exception:
            pass
        try:
            self.manager.interrupt()
        except Exception:
            pass
        self.busy = False
        self._note("Đã dừng.")

    async def on_key(self, event):
        k = getattr(event, "key", "")
        if k == "tab":
            event.prevent_default()
            event.stop()
            self._slash("/plan" if self.presets == "build" else "/build")
        elif k == "escape":
            if self.busy:
                event.prevent_default()
                event.stop()
                self._stop_all()


def run(manager):
    RemTUI(manager).run()
