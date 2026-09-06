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
VERSION = "3.10"

LOGO = r"""
 ____  _____ __  __ 
|  _ \| ____|  \/  |
| |_) |  _| | |\/| |
|  _ <| |___| |  | |
|_| \_\_____|_|  |_|
"""
DEBUG = os.environ.get("REM_DEBUG", "0") == "1"
MODEL_CHAT = "qwen/qwen3.8-27b"
MODEL_CLONE = "openai/gpt-oss-120b"
MODEL_VISION = "meta-llama/llama-3.2-11b-vision-instruct"
MODEL_FB = ["openai/gpt-oss-20b", "qwen/qwen3-32b", "meta-llama/llama-4-scout-17b-16e-instruct"]
MODEL_PREF_CHAT = [
    "qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
    "qwen/qwen3-32b", "llama-3.3-70b-versatile", "qwen/qwen3-8b",
]
MODEL_PREF_CLONE = [
    "openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b",
    "qwen/qwen3-32b", "llama-3.3-70b-versatile", "qwen/qwen3-8b",
]
MODEL_PREF_VISION = [
    "meta-llama/llama-3.2-11b-vision-instruct", "openai/gpt-oss-120b",
    "openai/gpt-oss-20b", "qwen/qwen3.8-27b",
]
MODEL_PREF_FB = [
    "openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b",
    "qwen/qwen3-32b", "meta-llama/llama-4-scout-17b-16e-instruct",
    "groq/compound", "groq/compound-mini", "llama-3.3-70b-versatile",
]
CTX_TOTAL = 20000       # tổng ký tự tối đa context sau trim (giới hạn cứng chống tràn)
CTX_CAP = 5000          # độ dài tối đa mỗi message content
SUM_AT = 60             # tóm tắt khi số message trong history vượt ngưỡng này
SUM_BUDGET = 90000      # tóm tắt khi tổng ký tự context vượt ngưỡng này
MAX_STEPS = 25          # số bước tool tối đa mỗi đợt (turn) — hết thì tự chuyển đợt mới
MAX_TURNS = 8           # số đợt tối đa 1 lượt câu hỏi (8 x 25 = 200 bước) — chống loop vô tận
CHECKPOINT_EVERY = 8    # lưu checkpoint ra file mỗi N bước
SHELL_TIMEOUT = 60
MAX_TOOL_OUT = 3000    # kết quả tool bị cắt ở mức này trước khi vào history
MAX_TRACE = 2000