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
VERSION = "3.0"
DEBUG = os.environ.get("REM_DEBUG", "0") == "1"
MODEL_CHAT = "qwen/qwen3.8-27b"
MODEL_CLONE = "llama-3.3-70b-versatile"
MODEL_VISION = "meta-llama/llama-3.2-11b-vision-instruct"
MODEL_FB = ["openai/gpt-oss-20b", "qwen/qwen3-32b", "meta-llama/llama-4-scout-17b-16e-instruct"]
CTX_MSGS = 12
CTX_CHARS = 900
SUM_AT = 40
SHELL_TIMEOUT = 60
MAX_TOOL_OUT = 3000
MAX_TRACE = 2000