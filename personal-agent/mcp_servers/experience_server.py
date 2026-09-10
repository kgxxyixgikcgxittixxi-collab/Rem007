"""experience_server — MCP wrapper cho hệ thống kinh nghiệm (experience.py).

Tools:
- exp_lesson_save: lưu bài học từ lỗi (Reflexion pattern)
- exp_lesson_find: tìm bài học phù hợp (chỉ nạp cái relevant, chống tràn context)
- exp_error_patterns: xem pattern lỗi lặp lại
- exp_skill_save / exp_skill_best: procedural memory
"""

import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcplib import Server, Tool, schema
import experience as _exp


def exp_lesson_save(task, error, critique, lesson, tags="", a=None):
    return _exp.record_lesson(task, error, critique, lesson, tags=tags)


def exp_lesson_find(query="", tags="", a=None):
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or None
    return _exp.recall_lessons(query=query, tags=tag_list)


def exp_error_patterns(a=None):
    return _exp.get_error_patterns()


def exp_skill_save(task_type, strategy, result="success", tags="", a=None):
    return _exp.record_skill(task_type, strategy, result, tags=tags)


def exp_skill_best(task_type, tags="", a=None):
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or None
    best = _exp.find_best_skill(task_type, tags=tag_list)
    return best or "(chưa có skill phù hợp)"


TOOLS = [
    Tool("exp_lesson_save", "LƯU BÀI HỌC từ lỗi: task + error + critique + lesson ngắn gọn.",
         schema({"task": {"type": "string"}, "error": {"type": "string"},
                 "critique": {"type": "string"}, "lesson": {"type": "string"},
                 "tags": {"type": "string", "default": ""}}),
         exp_lesson_save),
    Tool("exp_lesson_find", "TÌM BÀI HỌC phù hợp với task hiện tại (compact, chống tràn context).",
         schema({"query": {"type": "string", "default": ""},
                 "tags": {"type": "string", "default": ""}}),
         exp_lesson_find),
    Tool("exp_error_patterns", "XEM pattern lỗi lặp lại nhiều nhất.",
         schema({}), exp_error_patterns),
    Tool("exp_skill_save", "LƯU procedural skill: chiến lược nào hiệu quả cho loại task nào.",
         schema({"task_type": {"type": "string"}, "strategy": {"type": "string"},
                 "result": {"type": "string", "default": "success"},
                 "tags": {"type": "string", "default": ""}}),
         exp_skill_save),
    Tool("exp_skill_best", "TÌM chiến lược tốt nhất cho loại task (theo success_rate).",
         schema({"task_type": {"type": "string"}, "tags": {"type": "string", "default": ""}}),
         exp_skill_best),
]

if __name__ == "__main__":
    Server(TOOLS, "experience", "0.1.0").serve(sys.stdin, sys.stdout)
