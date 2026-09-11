"""skills_server — hệ thống SKILL tự tiến hoá (lấy ý tưởng GenericAgent).

Mỗi lần agent hoàn thành task thành công, chuỗi tool đã dùng được lưu thành
một Skill có thể tái sử dụng. Lần sau gặp task tương tự, skill được nạp thẳng
vào system prompt → agent làm nhanh hơn, ít tool hơn, ít lỗi hơn.

Dữ liệu lưu ở: ~/.rem_ai/skills/<tên>.json
Điểm khác GenericAgent: không cần LLM tổng hợp thêm — lưu thẳng chuỗi tool
cùng kết quả, tự tin hơn và không tốn thêm token.
"""

import os, sys, time, re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json, threading
from mcplib import Server, Tool, schema
from config import DIR

SKILL_DIR = os.path.join(DIR, "skills")
os.makedirs(SKILL_DIR, exist_ok=True)
_lock = threading.Lock()
_MAX_STEPS_IN_PROMPT = 12


def _slug(name):
    """Tên skill → slug an toàn cho tên file."""
    s = re.sub(r"[^\w\s-]", "", name.lower()).strip()
    return re.sub(r"[\s]+", "_", s)[:60] or "skill"


def _load(name):
    fp = os.path.join(SKILL_DIR, _slug(name) + ".json")
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save(skill):
    fp = os.path.join(SKILL_DIR, _slug(skill["name"]) + ".json")
    from mcplib import atomic_write_json
    atomic_write_json(fp, skill)


def _all_skills():
    out = []
    for fn in sorted(os.listdir(SKILL_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SKILL_DIR, fn), "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            pass
    return out


def skill_save(name, description, steps, tags="", a=None):
    """Lưu 1 skill với chuỗi bước đã thực hiện thành công.

    steps: list [{"tool": tên, "args": {...}, "result_ok": bool}]
    """
    with _lock:
        existing = _load(name) or {}
        times = existing.get("times", 0)
        skill = {
            "name": name.strip()[:80],
            "description": (description or "").strip()[:500],
            "tags": [t.strip() for t in (tags or "").split(",") if t.strip()][:10],
            "steps": steps[:_MAX_STEPS_IN_PROMPT],
            "created": existing.get("created", time.strftime("%Y-%m-%d %H:%M")),
            "updated": time.strftime("%Y-%m-%d %H:%M"),
            "times": times + 1,
        }
        _save(skill)
    return f"Đã lưu skill '{skill['name']}' ({len(steps)} bước, dùng lần {skill['times']})."


def skill_find(keyword="", a=None):
    """Tìm skill khớp keyword (tên/desc/tags). Rỗng = liệt kê tất cả."""
    kw = (keyword or "").strip().lower()
    out = []
    for s in _all_skills():
        hay = (s.get("name", "") + " " + s.get("description", "") + " " + " ".join(s.get("tags", []))).lower()
        if kw and kw not in hay:
            continue
        n = len(s.get("steps", []))
        out.append(f"skill:{s['name']} — {s.get('description', '')[:120]} ({n} bước, dùng {s.get('times', 0)} lần)")
    return "\n".join(out) if out else "(chưa có skill nào khớp — dùng skill_save để lưu sau khi hoàn thành task)"


def skill_use(name, a=None):
    """Lấy chi tiết skill để dùng lại."""
    s = _load(name)
    if not s:
        return f"Không có skill '{name}'. Xem skill_find để liệt kê."
    lines = [f"# Skill: {s['name']}", f"# {s.get('description', '')}", f"# Dùng {s.get('times', 0)} lần — cập nhật {s.get('updated', '')}"]
    for i, st in enumerate(s.get("steps", []), 1):
        t = st.get("tool", "?")
        args = st.get("args", {})
        lines.append(f"{i}. {t}({str(args)[:200]})")
    return "\n".join(lines)


def skill_list(a=None):
    """Liệt kê tất cả skill đã học."""
    out = _all_skills()
    if not out:
        return "(chưa có skill nào — agent sẽ tự học sau mỗi task thành công)"
    return "\n".join(f"- {s['name']}: {s.get('description', '')[:100]} ({len(s.get('steps', []))} bước, dùng {s.get('times', 0)} lần)" for s in out)


def _inject_prompt():
    """Thêm skill đã học vào system prompt (gọi từ agentloop). Trả đoạn text."""
    out = _all_skills()
    if not out:
        return ""
    lines = ["SKILL ĐÃ HỌC (dùng lại khi gặp task tương tự — LÀM THEO các bước này để nhanh hơn):"]
    for s in out[:8]:
        steps = s.get("steps", [])[:6]
        brief = " -> ".join(f"{st.get('tool', '?')}" for st in steps)
        lines.append(f"- {s['name']}: {brief}")
    return "\n".join(lines)


TOOLS = [
    Tool("skill_save", "LƯU SKILL: lưu chuỗi bước đã thực hiện THÀNH CÔNG thành skill tái sử dụng. "
         "Gọi SAU khi hoàn thành task thành công để agent tự học.",
         schema({"name": {"type": "string", "description": "tên skill ngắn gọn (vd 'tao_game_snake')"},
                 "description": {"type": "string", "description": "mô tả ngắn skill làm gì"},
                 "steps": {"type": "array", "description": "list bước: [{tool, args, result_ok}]",
                           "items": {"type": "object"}},
                 "tags": {"type": "string", "default": ""}}),
         skill_save),
    Tool("skill_find", "TÌM SKILL đã học khớp keyword. Dùng khi bắt đầu task mới.",
         schema({"keyword": {"type": "string", "default": ""}}),
         skill_find),
    Tool("skill_use", "LẤY CHI TIẾT skill đã học để thực hiện lại từng bước.",
         schema({"name": {"type": "string"}}),
         skill_use),
    Tool("skill_list", "LIỆT KÊ tất cả skill đã học.",
         schema({}), skill_list),
]


if __name__ == "__main__":
    Server(TOOLS, "skills", "0.1.0").serve(sys.stdin, sys.stdout)