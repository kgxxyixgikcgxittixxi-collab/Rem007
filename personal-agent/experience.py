"""experience — Hệ thống kinh nghiệm tự học kiểu Reflect + agent-memory.

Mục tiêu: Rem Agent học từ mỗi task — thành công thì lưu skill, thất bại thì lưu lesson.
Tránh lặp lại lỗi lần sau. Chỉ nhớ cái quan trọng — không tràn context.

Thiết kế:
- Procedural Memory: lưu "chiến lược nào thành công" cho từng loại task
- Lessons: lưu "lỗi nào đã gặp, cách fix là gì" — Reflexion paper pattern
- Ebbinghaus Decay: nhớ cũ phai dần, nhớ mới nổi bật
- Hybrid Retrieval: relevance × recency × importance — chỉ nạp relevant vào context
- Compact format: mỗi lesson chỉ ~200 token — tránh tràn context
"""

import os, sys, time, json, re, hashlib, threading
from datetime import datetime, timedelta
from collections import deque
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

EXP_DIR = os.path.join(config.DIR, "experience")
os.makedirs(EXP_DIR, exist_ok=True)
_LESSONS_FILE = os.path.join(EXP_DIR, "lessons.json")
_SKILLS_FILE = os.path.join(EXP_DIR, "skills.json")
_lock = threading.Lock()

# Context budget: chỉ nạp tối đa 8 lessons + 4 skills vào prompt
MAX_LESSONS_IN_PROMPT = 8
MAX_SKILLS_IN_PROMPT = 4

# Ebbinghaus decay parameters
DECAY_RATE = 0.15  # tốc độ quên
STABILITY_BOOST = 0.3  # mỗi lần recall tăng stability


# ── Lesson (from Reflexion paper) ────────────────────────────────

def _load_lessons():
    try:
        with open(_LESSONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"lessons": [], "next_id": 1}


def _save_lessons(data):
    # Ghi atomic (tmp + replace): nhiều tiến trình Remtm/MCP cùng viết file này,
    # ghi trực tiếp dễ rách file/mất cập nhật khi crash giữa chừng.
    tmp = _LESSONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _LESSONS_FILE)


def record_lesson(task, error, critique, lesson, tags=None, outcome="failure"):
    """Ghi 1 lesson từ lỗi — Reflexion pattern.
    
    task: mô tả task đang làm
    error: thông báo lỗi thực tế
    critique: phân tích vì sao sai
    lesson: bài học rút ra (200 ký tự trở xuống)
    tags: danh mục ["python", "bash", "api", ...]
    outcome: "failure" or "success"
    """
    with _lock:
        data = _load_lessons()
        lesson_id = data["next_id"]
        data["next_id"] += 1
        
        entry = {
            "id": lesson_id,
            "task": task[:200],
            "error": error[:300],
            "critique": critique[:300],
            "lesson": lesson[:200],  # Compact — tránh tràn context
            "tags": [t.strip() for t in (tags or "").split(",") if t.strip()][:10],
            "outcome": outcome,
            "created": datetime.now().isoformat(),
            "last_accessed": None,
            "times_recalled": 0,
            "stability": 0.5,  # Laplace prior: starts neutral
            "importance": 0.5,
        }
        
        # Dedup: kiểm tra lesson tương tự
        for existing in data["lessons"]:
            if _similar(existing["lesson"], lesson):
                existing["times_recalled"] += 1
                existing["stability"] = min(1.0, existing["stability"] + 0.1)
                _save_lessons(data)
                return f"Lesson #{lesson_id} deduped into existing #{existing['id']}"
        
        data["lessons"].append(entry)
        
        # Giới hạn 50 lessons — cũ bị xóa
        if len(data["lessons"]) > 50:
            data["lessons"] = sorted(data["lessons"], key=lambda x: x["stability"], reverse=True)[:50]
        
        _save_lessons(data)
    return f"Lesson #{lesson_id} saved: {lesson[:60]}..."


def _similar(a, b):
    """Kiểm tra 2 lesson có tương tự không (normalized Levenshtein)."""
    if not a or not b:
        return False
    # Normalize: lowercase, remove punctuation
    na = re.sub(r"[^a-z0-9]", "", a.lower())
    nb = re.sub(r"[^a-z0-9]", "", b.lower())
    if len(na) < 3 or len(nb) < 3:
        return False
    # Simple Jaccard-like similarity
    set_a = set(na[i:i+4] for i in range(len(na)-3))
    set_b = set(nb[i:i+4] for i in range(len(nb)-3))
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union > 0.6 if union > 0 else False


def recall_lessons(query="", tags=None, limit=MAX_LESSONS_IN_PROMPT, touch=True):
    """Hybrid retrieval: relevance × recency × importance × stability.

    Chỉ trả về lessons RELEVANT — tránh tràn context.
    touch=False: chỉ đọc preview cho system prompt (không tăng recall count,
    không ghi file) để tránh spam write mỗi step.
    """
    with _lock:
        data = _load_lessons()
    
    if not data["lessons"]:
        return "(chưa có lesson nào)"
    
    kw = (query or "").strip().lower()
    tag_set = set(tags) if tags else None
    
    scored = []
    now = time.time()
    for lesson in data["lessons"]:
        score = 0.0
        
        # Relevance: keyword match
        if kw:
            hay = (lesson["task"] + " " + lesson["lesson"] + " " + " ".join(lesson.get("tags", []))).lower()
            if kw in hay:
                score += 0.4
        
        # Tag match
        if tag_set:
            if any(t in lesson.get("tags", []) for t in tag_set):
                score += 0.3
        
        # Recency (decay theo thời gian)
        days_old = (now - datetime.fromisoformat(lesson["created"]).timestamp()) / 86400
        decay = max(0, 1.0 / (1.0 + DECAY_RATE * days_old))
        score += 0.1 * decay
        
        # Stability (ít bị quên = quan trọng)
        score += 0.2 * lesson.get("stability", 0.5)
        
        # Times recalled (được nhắc lại nhiều = quan trọng)
        score += 0.05 * min(lesson.get("times_recalled", 0), 10)
        
        if score > 0:
            scored.append((score, lesson))
    
    # Sort by score, limit
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [s[1] for s in scored[:limit]]
    
    if not top:
        return "(không có lesson phù hợp)"
    
    # Format compact — mỗi lesson ~100 token
    out = []
    for l in top:
        tags_str = ", ".join(l.get("tags", []))[:40]
        out.append(f"[L{l['id']}] {l['lesson'][:100]} | tags: {tags_str}")

    # Ghi nhận recall: nạp mới theo id DƯỚI KHÓA (tránh mất cập nhật khi nhiều
    # tiến trình cùng chạm — trước đây đọc/sửa/lưu ngoài khóa nên ghi đè nhau)
    if touch:
        with _lock:
            data2 = _load_lessons()
            by_id = {x.get("id"): x for x in data2.get("lessons", [])}
            for l in top:
                cur = by_id.get(l.get("id"))
                if cur is None:
                    continue
                cur["last_accessed"] = datetime.now().isoformat()
                cur["times_recalled"] = cur.get("times_recalled", 0) + 1
                cur["stability"] = min(1.0, cur.get("stability", 0.5) + STABILITY_BOOST * 0.1)
            _save_lessons(data2)
    return "\n".join(out)


def get_error_patterns():
    """Xem patterns lỗi lặp lại."""
    with _lock:
        data = _load_lessons()
    
    patterns = {}
    for l in data["lessons"]:
        # Extract first "word" from error as pattern key
        err_key = re.sub(r"[^a-z]", "", l["error"][:50].lower())[:20]
        if err_key:
            patterns[err_key] = patterns.get(err_key, 0) + 1
    
    if not patterns:
        return "(chưa có pattern)"
    
    return "\n".join(f"  {k}: {v} lần" for k, v in sorted(patterns.items(), key=lambda x: -x[1])[:10])


# ── Procedural Memory (from agent-memory) ──────────────────────

def _load_skills():
    try:
        with open(_SKILLS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"skills": [], "next_id": 1}


def _save_skills(data):
    tmp = _SKILLS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _SKILLS_FILE)


def record_skill(task_type, strategy, result, tags=""):
    """Ghi 1 procedural skill: chiến lược nào thành công cho task_type."""
    with _lock:
        data = _load_skills()
        # Dedup: cùng task_type + strategy → chỉ tăng times_used (trước đây mỗi
        # task thành công tạo 1 bản trùng, file phình vô hạn với toàn "general")
        for s in data.get("skills", []):
            if s.get("task_type") == (task_type or "")[:80] and s.get("strategy") == (strategy or "")[:200]:
                s["times_used"] = s.get("times_used", 0) + 1
                _save_skills(data)
                return f"Skill #{s['id']} reused (dùng lần {s['times_used']})"
        skill_id = data["next_id"]
        data["next_id"] += 1
        
        entry = {
            "id": skill_id,
            "task_type": task_type[:80],
            "strategy": strategy[:200],
            "result": result[:100],  # "success" or "failure"
            "tags": [t.strip() for t in tags.split(",") if t.strip()][:5],
            "created": datetime.now().isoformat(),
            "times_used": 0,
            "success_rate": 0.5,  # Laplace prior
        }
        data["skills"].append(entry)
        
        # Giới hạn 30 skills
        if len(data["skills"]) > 30:
            data["skills"] = sorted(data["skills"], key=lambda x: x["success_rate"], reverse=True)[:30]
        
        _save_skills(data)
    return f"Skill #{skill_id}: {task_type} → {strategy[:40]}..."


def find_best_skill(task_type, tags=None):
    """Tìm skill tốt nhất cho task_type dựa trên success_rate."""
    with _lock:
        data = _load_skills()
    
    candidates = [s for s in data["skills"] if s["task_type"] == task_type]
    if tags:
        tag_set = set(tags)
        candidates = [s for s in candidates if any(t in s.get("tags", []) for t in tag_set)]
    
    if not candidates:
        return None
    
    # Sort by success_rate
    candidates.sort(key=lambda x: x.get("success_rate", 0.5), reverse=True)
    best = candidates[0]

    # Chỉ đọc, KHÔNG tăng times_used ở đây (trước đây mỗi lần tra cứu cũng tính
    # là "dùng" làm số liệu phình; việc dùng thật do update_skill_result ghi nhận)
    return f"[S{best['id']}] {best['strategy'][:100]} (success_rate: {best.get('success_rate', 0.5):.1f})"


def update_skill_result(skill_id, success):
    """Ghi nhận kết quả dùng skill (Laplace smoothing), CÓ lưu file.
    (Trước đây hàm này tính sai công thức VÀ quên save nên success_rate đứng yên.)"""
    with _lock:
        data = _load_skills()
        for s in data.get("skills", []):
            if s.get("id") == skill_id:
                s["successes"] = s.get("successes", 0) + (1 if success else 0)
                s["times_used"] = s.get("times_used", 0) + 1
                s["success_rate"] = (s["successes"] + 1) / (s["times_used"] + 2)
                _save_skills(data)
                return s["success_rate"]
    return None


def get_skills_prompt():
    """Inject best skills into system prompt (compact, avoid context overflow)."""
    with _lock:
        data = _load_skills()
    
    top = sorted(data["skills"], key=lambda x: x["success_rate"], reverse=True)[:MAX_SKILLS_IN_PROMPT]
    if not top:
        return ""
    
    lines = ["SKILLS ĐÃ HỌC (dùng chiến lược này cho task tương tự):"]
    for s in top:
        lines.append(f"- {s['task_type']}: {s['strategy'][:80]} (success: {s['success_rate']:.0%})")
    return "\n".join(lines)


# ── Auto-Learn After Task ────────────────────────────────────────

def auto_learn(task_description, tool_steps, success, error_msg=""):
    """Tự động học từ task kết quả.
    
    Gọi sau mỗi task: nếu thành công → save skill; nếu thất bại → save lesson.
    """
    if success and tool_steps:
        # Lưu procedural skill
        # Xác định task_type từ description
        task_type = "general"
        desc_lower = task_description.lower()
        for t in ["game", "code", "web", "video", "music", "presentation", "social"]:
            if t in desc_lower:
                task_type = t
                break
        
        # Trích xuất strategy quan trọng nhất
        tool_names = [st.get("tool", "?") for st in tool_steps if st.get("result_ok")]
        strategy = " → ".join(tool_names[-3:]) if tool_names else "unknown"
        
        record_skill(task_type, strategy, "success", tags=task_type)
        return f"Skill saved for {task_type}: {strategy}"
    
    elif not success and error_msg:
        # Lưu lesson (Reflexion pattern)
        lesson = extract_lesson(task_description, error_msg)
        if lesson:
            record_lesson(task_description, error_msg, "Pattern detected", lesson, tags="general")
            return f"Lesson saved: {lesson[:60]}..."
    
    return None


def extract_lesson(task, error):
    """Trích xuất lesson từ error message — đơn giản hóa."""
    if not error:
        return None
    
    # Common patterns
    patterns = [
        (r"ModuleNotFoundError|ImportError", "Always import modules before using them. Check pip install."),
        (r"SyntaxError", "Check syntax carefully. Use py_compile before running."),
        (r"429|rate.?limit", "Too many requests. Add delay between calls or use backoff."),
        (r"connection.*refused|timeout", "Check if server is running. Verify port and process."),
        (r"FileNotFound|No such file", "Verify file paths. Use os.path.exists() first."),
        (r"Permission.*denied", "Check file permissions. Use chmod or run as appropriate user."),
        (r"JSONDecodeError|invalid.*JSON", "Validate JSON before parsing. Use json.loads() with try/except."),
        (r"MemoryError|out of memory", "Reduce batch size or increase available memory."),
    ]
    
    for pattern, lesson_text in patterns:
        if re.search(pattern, error, re.I):
            return lesson_text
    
    # Default: generic lesson
    return f"Task failed. Error: {error[:100]}. Debug and retry with different approach."


# ── Context Budget Manager ───────────────────────────────────────

def get_experience_prompt(preview=True):
    """Generate compact prompt section — only relevant lessons + best skills."""
    parts = []

    # Best skills
    skills_prompt = get_skills_prompt()
    if skills_prompt:
        parts.append(skills_prompt)

    # Top 8 lessons (already filtered by relevance in recall_lessons)
    lessons_prompt = recall_lessons(limit=MAX_LESSONS_IN_PROMPT, touch=not preview)
    if lessons_prompt and not lessons_prompt.startswith("("):
        parts.append("LESSONS LEARNED (tránh lặp lỗi):\n" + lessons_prompt)
    
    if not parts:
        return ""
    
    return "\n\n".join(parts)


# ── Init ─────────────────────────────────────────────────────────

def init_experience():
    """Initialize experience system. Call at startup."""
    # Ensure directories and files exist
    os.makedirs(EXP_DIR, exist_ok=True)
    if not os.path.exists(_LESSONS_FILE):
        _save_lessons({"lessons": [], "next_id": 1})
    if not os.path.exists(_SKILLS_FILE):
        _save_skills({"skills": [], "next_id": 1})
    return f"Experience system initialized. {len(_load_lessons()['lessons'])} lessons, {len(_load_skills()['skills'])} skills."


# Print summary on import
if __name__ != "__main__":
    init_experience()