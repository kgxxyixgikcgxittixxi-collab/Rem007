import os, platform as PF
HOME = os.path.expanduser("~")
DIR = os.path.join(HOME, ".rem_ai"); os.makedirs(DIR, exist_ok=True)
TMP = os.path.join(DIR, "tmp"); os.makedirs(TMP, exist_ok=True)
WEB = os.path.join(DIR, "web"); os.makedirs(WEB, exist_ok=True)
TERMUX = "com.termux" in os.environ.get("PREFIX", "") or os.path.exists("/data/data/com.termux/files/usr")
WIN = PF.system() == "Windows"
MAC = PF.system() == "Darwin"
PC = not TERMUX
NAME = "Rem Agent"
VERSION = "3.27"

LOGO = r"""
 ____  _____ __  __ 
|  _ \| ____|  \/  |
| |_) |  _| | |\/| |
|  _ <| |___| |  | |
|_| \_\_____|_|  |_|
"""
DEBUG = os.environ.get("REM_DEBUG", "0") == "1"
MODEL_CHAT = "openai/gpt-oss-120b"
MODEL_CLONE = "openai/gpt-oss-120b"
MODEL_VISION = "meta-llama/llama-3.2-11b-vision-instruct"
MODEL_FB = ["openai/gpt-oss-20b", "qwen/qwen3-32b", "meta-llama/llama-4-scout-17b-16e-instruct"]
MODEL_PREF_CHAT = [
    "openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b",
    "qwen/qwen3-8b",
]
MODEL_PREF_CLONE = [
    "openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b",
    "qwen/qwen3-8b",
]
MODEL_PREF_VISION = [
    "meta-llama/llama-3.2-11b-vision-instruct", "openai/gpt-oss-120b",
    "openai/gpt-oss-20b", "qwen/qwen3.8-27b",
]
MODEL_PREF_FB = [
    "openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b",
    "meta-llama/llama-4-scout-17b-16e-instruct", "qwen/qwen3-8b",
    "groq/compound", "groq/compound-mini",
]
CTX_TOTAL = 20000       # tổng ký tự tối đa context sau trim (giới hạn cứng chống tràn)
CTX_CAP = 5000          # độ dài tối đa mỗi message content
SUM_AT = 60             # tóm tắt khi số message trong history vượt ngưỡng này
SUM_BUDGET = 90000      # tóm tắt khi tổng ký tự context vượt ngưỡng này
MAX_STEPS = 12          # số bước tool tối đa mỗi đợt (turn) — đủ thông tin là phải dừng, không lang thang quét thêm
MAX_TURNS = 8           # số đợt tối đa 1 lượt câu hỏi (8 x 25 = 200 bước) — chống loop vô tận
CHECKPOINT_EVERY = 8    # lưu checkpoint ra file mỗi N bước
SHELL_TIMEOUT = 60
MAX_TOOL_OUT = 3000    # kết quả tool bị cắt ở mức này trước khi vào history
MAX_TRACE = 2000
MAX_TASK_SECONDS = 300   # tổng thời gian tối đa 1 câu hỏi → quá thì tự dừng (chống treo)
                         # 300s đủ cho dự án lớn nhiều tool; stream + watchdog vẫn chống treo
TOOL_TIMEOUT_FAST = 90   # timeout MCP cho tool thường (bash/đọc/ghi/web...)
TOOL_TIMEOUT_PKG = 600   # timeout cho pip_install/ensure_tool (cài đặt lâu)
TOOL_SLOW_WARN = 40      # sau N giây 1 bước chưa xong → spinner chuyển đỏ cảnh báo treo

# ── repository + tự cập nhật bản mới từ GitHub ────────────────────────────
GIT_REPO = "kgxxyixgikcgxittixxi-collab/Rem007"
GIT_BRANCH = "main"
REMOTE_CONFIG = f"https://raw.githubusercontent.com/{GIT_REPO}/{GIT_BRANCH}/personal-agent/config.py"
REPO_TARBALL = f"https://github.com/{GIT_REPO}/archive/refs/heads/{GIT_BRANCH}.tar.gz"
AUTO_UPDATE = os.environ.get("REM_AUTO_UPDATE", "1") == "1"  # REM_AUTO_UPDATE=0 để chỉ báo, không tự update
UPDATE_CHECK_TIMEOUT = 6    # giây tối đa chờ kiểm tra bản mới
UPDATE_TIMEOUT = 120        # giây tối đa cho 1 lần cập nhật