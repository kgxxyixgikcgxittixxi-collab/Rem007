# Render Markdown sang chuỗi ANSI rõ nét kiểu opencode — nội dung chữ VẪN TRẮNG,
# chỉ tô màu làm nổi cấu trúc: tiêu đề / bold / code / bullet / bảng / trích dẫn.

import re

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
IT = "\033[3m"
REV = "\033[7m"
BL = "\033[94m"
CY = "\033[96m"
YE = "\033[93m"
GR = "\033[92m"
RD = "\033[91m"
MG = "\033[95m"
WH = "\033[37m"
BG_CODE = "\033[48;5;235m"  # nền code tối nhẹ để phân biệt khối code

_ESC = re.compile(r"\x1b\[[0-9;]*m")
# ký tự Đông Á rộng 2 cột (CJK/full-width) → canh lề không bị lệch
_WIDE = re.compile(r"[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]")

_TOK = re.compile(r"\x1b\[[0-9;]*m|\s+|\S+")

FENCE = re.compile(r"^\s*```([\w+-]*)")
HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
RULE = re.compile(r"^\s*---+\s*$")
QUOTE = re.compile(r"^\s*>\s?(.*)$")
LIST = re.compile(r"^(\s*)([-*]|•|\d+[.)])\s+(.*)$")
TBL_HEAD = re.compile(r"^\s*\|?\s*:?-{3,}")
TBL_ROW = re.compile(r"^\s*\|")


def term_width():
    try:
        import os
        return os.get_terminal_size().columns or 90
    except Exception:
        return 90


def disp_len(s):
    """Độ rộng hiển thị của chuỗi (Đông Á = 2 cột), bỏ qua mã ANSI."""
    n = 0
    for m in _ESC.split(s):
        n += len(_WIDE.sub(lambda _: "__", m))
    return n


def _strip_inline(s):
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
    return s.replace("~~", "")


def _inline(s):
    """Tô inline trên 1 dòng: `code`→reverse, **bold**, *italic*, [text](url)."""
    s = re.sub(r"`([^`]+)`", lambda m: REV + m.group(1) + RESET, s)
    s = re.sub(r"\*\*([^*]+)\*\*", lambda m: BOLD + m.group(1) + RESET, s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", lambda m: IT + m.group(1) + RESET, s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: m.group(1) + " " + DIM + m.group(2) + RESET, s)
    s = re.sub(r"\\([#*`_])", r"\1", s)
    return s.replace("~~", "")


def _tokens(s):
    """Tách chuỗi ANSI thành (loại, chuỗi): esc | s (khoảng trắng) | w (từ có mã màu kèm)."""
    raw = []
    for m in _TOK.finditer(s):
        t = m.group(0)
        if t.startswith("\x1b"):
            raw.append(("esc", t))
        elif t.isspace():
            raw.append(("s", t))
        else:
            raw.append(("w", t))
    out = []
    pend = ""
    for kind, t in raw:
        if kind == "esc":
            pend += t
        elif kind == "s":
            if pend:
                out.append(("esc", pend))
                pend = ""
            out.append(("s", t))
        else:
            out.append(("w", pend, t))
            pend = ""
    if pend:
        out.append(("esc", pend))
    return out


def _wrap_ansi(text, tw):
    """Xuống dòng theo cột thật, KHÔNG cắt giữa mã màu / từ."""
    out, line, llen = [], [], 0

    def flush():
        nonlocal line, llen
        if line:
            out.append("".join(line))
        line, llen = [], 0

    for tok in _tokens(text):
        if tok[0] == "esc":
            line.append(tok[1])
        elif tok[0] == "s":
            continue
        else:
            w = (tok[1] or "") + tok[2]
            wlen = disp_len(w)
            if llen and llen + 1 + wlen > tw:
                flush()
            if llen:
                line.append(" ")
                llen += 1
            line.append(w)
            llen += wlen
    flush()
    return out


def _heading(line):
    m = HEAD.match(line)
    if not m:
        return None
    n = len(m.group(1))
    col = [BL, CY, GR, YE, MG, RD][min(n, 6) - 1]
    return BOLD + col + m.group(2).strip() + RESET


def _rule(tw):
    return DIM + ("─" * max(12, tw - 2)) + RESET


def _blockquote(line, tw):
    body = _inline(line.strip()[1:].strip())
    ws = _wrap_ansi(body, tw - 4)
    return ["  " + YE + "▎" + RESET + DIM + l + RESET for l in (ws or [" "])]


def _code_block(body, lang="", tw=90):
    out = ["   " + DIM + "┌ " + (lang or "code") + (" " * max(0, tw - 8 - len(lang or "code"))) + "┐" + RESET]
    for ln in body:
        out.append(BG_CODE + "   " + (ln if ln else " ") + RESET)
    out.append("   " + DIM + "└" + ("─" * max(0, tw - 7)) + "┘" + RESET)
    return out


def _table(rows, tw):
    widths = []
    for rr in rows:
        for i, c in enumerate(rr):
            w = disp_len(_strip_inline(c))
            if len(widths) <= i:
                widths.append(0)
            widths[i] = max(widths[i], w)
    widths = [min(w, 26) for w in widths]
    seg = ["─" * (w + 2) for w in widths]
    out = ["  " + DIM + "┌" + "┬".join(seg) + "┐" + RESET]
    for ri, rr in enumerate(rows):
        cells = []
        for i, c in enumerate(rr):
            pad = (widths[i] if i < len(widths) else 0) - disp_len(_strip_inline(c))
            cells.append(" " + _inline(c) + " " * max(0, pad) + " ")
        line = "  " + CY + "│" + "│".join(cells) + "│" + RESET
        if ri == 1:
            out.append("  " + DIM + "├" + "┼".join(seg) + "┤" + RESET)
        out.append(line)
    out.append("  " + DIM + "└" + "┴".join(seg) + "┘" + RESET)
    return out


def split_thinking(text):
    """Tách phần  thinking ... response ra khỏi câu trả lời sạch.
    Robust: thẻ mở phải đúng dòng; thẻ đóng chấp nhận cả dạng dính liền
    (vd 'response' ngay đầu dòng câu trả lời)."""
    if not text:
        return "", text or ""
    lines = text.split("\n")
    s = None
    for i, ln in enumerate(lines):
        t = ln.strip().lower()
        if t == "thinking" or t.startswith("<think"):
            s = i
            break
    if s is None:
        return "", text.strip()
    e = None
    for i in range(s + 1, len(lines)):
        tt = lines[i].strip().lower()
        if tt == "response" or tt.startswith("response ") or tt.startswith("</think"):
            e = i
            break
    before = "\n".join(lines[:s])
    if e is None:
        think = "\n".join(lines[s + 1:])
        after = ""
    else:
        think = "\n".join(lines[s + 1:e])
        rest = re.sub(r"^\s*response\s*", "", lines[e], flags=re.I)
        after = (rest + "\n" if rest.strip() else "") + "\n".join(lines[e + 1:])
    body = "\n".join(x for x in (before.strip(), after.strip()) if x)
    return think.strip(), body or ""


def md_to_ansi(text, tw=None):
    """Markdown → chuỗi ANSI đã xuống dòng sẵn. Nội dung chữ vẫn TRẮNG."""
    tw = tw or term_width()
    if not text:
        return ""
    lines = text.split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        m = FENCE.match(line)
        if m:
            j = i + 1
            body = []
            while j < n and not FENCE.match(lines[j]):
                body.append(lines[j].rstrip())
                j += 1
            out.extend(_code_block(body, m.group(1), tw))
            i = j + 1
            continue
        if line.lstrip().startswith(">"):
            out.extend(_blockquote(line, tw))
            i += 1
            continue
        if i + 1 < n and TBL_HEAD.match(lines[i + 1]):
            rows = [re.split(r"\s*\|\s*", l.strip().strip("|")) for l in (line, lines[i + 1])]
            j = i + 2
            while j < n and TBL_ROW.match(lines[j]):
                rows.append(re.split(r"\s*\|\s*", lines[j].strip().strip("|")))
                j += 1
            out.extend(_table([rows[0]] + rows[2:], tw))
            i = j
            continue
        h = _heading(line)
        if h:
            out.extend(_wrap_ansi(h, tw))
            i += 1
            continue
        if RULE.match(line):
            out.append(_rule(tw))
            i += 1
            continue
        ml = LIST.match(line)
        if ml:
            indent, mark, rest = ml.group(1), ml.group(2), ml.group(3)
            bullet = "•" if mark in ("-", "*", "•") else mark.rstrip(".)") + "."
            head = "  " * (len(indent) // 2) + GR + bullet + RESET + " "
            sub = "  " * (len(indent) // 2 + 1)
            ws = _wrap_ansi(_inline(rest.strip()), tw - disp_len(head))
            out.append(head + (ws[0] if ws else ""))
            for l in ws[1:]:
                out.append(sub + l)
            i += 1
            continue
        para = []
        while i < n and lines[i].strip() and not any(
            re.match(p, lines[i].lstrip()) for p in (r"^```", r"^#{1,6}\s", r"^---+\s*$", r"^>", r"^[-*•]\s|^\d+[.)]\s", r"^\|")
        ):
            para.append(lines[i].strip())
            i += 1
        body = _inline(" ".join(para))
        ws = _wrap_ansi(body, tw)
        out.extend(ws or [""])
    return "\n".join(out)


def thinking_to_ansi(text, tw=None, full=False):
    """Khối 'Đã suy luận' MỜ kiểu opencode. Mặc định CUỘN GỌN 1 dòng (như chế độ hide),
    chỉ hiển thị vài dòng tóm tắt; full=True in toàn bộ (dùng khối /think)."""
    tw = tw or term_width()
    if not text:
        return ""
    plain = re.sub(r"\s+", " ", _strip_inline(text)).strip() or "bài toán phức tạp"
    lines = [ln for ln in (l.strip() for l in text.split("\n")) if ln]
    if len(plain) <= 72 and len(lines) <= 2:
        full = True  # quá ngắn thì hiện luôn
    head = DIM + "─ " + YE + "Đã suy luận" + (f" · {plain[:72]}{'…' if len(plain) > 72 else ''}" if not full else "") + RESET
    out = [head]
    if full:
        for ln in lines:
            ws = _wrap_ansi(DIM + ln + RESET, tw - 4)
            for l in (ws or [DIM + " " + RESET]):
                out.append("   " + l)
        out.append(DIM + f"({len(lines)} dòng · {len(plain)} ký tự)" + RESET)
    else:
        extra = max(0, len(lines) - 1)
        if extra > 0:
            out.append(DIM + f"(suy luận {extra} dòng nữa — bấm /think để xem đầy đủ)" + RESET)
    return "\n".join(out)