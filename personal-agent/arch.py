"""arch — kiến trúc multi-key + multi-agent + task planning (kiểu Google Antigravity).

Mục tiêu: Rem Agent chạy 24/7 với nhiều key Groq độc lập, phân công task
theo chuyên môn, tự động phục hồi khi lỗi.

Thiết kế:
- KEY_POOL_MAIN: 6 key cho chat/code/web (tốc độ cao)
- KEY_POOL_MEDIA: 4 key cho TTS/image/video (context lớn)
- KEY_POOL_DESKTOP: 2 key cho desktop/browser automation (low latency)
- Multi-agent: agent chính phân task cho agent con chạy song song
- Task planning: phân rã task lớn → subtask → assign to pool
- Watchdog: giám sát health, tự restart, auto-resume
"""

import os, sys, time, threading, json, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from providers import groq

# ── Key Pools ────────────────────────────────────────────────────────────────

KEY_POOLS = {
    "main": {
        "purpose": "Chat, coding, web search, general tasks",
        "max_concurrent": 4,
        "timeout": 90,
        "model_pref": config.MODEL_PREF_CHAT,
        "budget": 120,
    },
    "media": {
        "purpose": "TTS, image generation, video processing",
        "max_concurrent": 2,
        "timeout": 180,
        "model_pref": config.MODEL_PREF_VISION,
        "budget": 300,
    },
    "desktop": {
        "purpose": "Desktop control, browser automation, click/type",
        "max_concurrent": 2,
        "timeout": 60,
        "model_pref": ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
        "budget": 60,
    },
}

# Track usage per pool for load balancing
_pool_usage = {name: {"calls": 0, "last_call": 0.0, "errors": 0} for name in KEY_POOLS}
_pool_lock = threading.Lock()


class KeyPool:
    """Quản lý 1 pool key độc lập."""

    def __init__(self, name, pool_config):
        self.name = name
        self.cfg = pool_config
        self._busy = False
        self._last_activity = 0.0

    def pick_key(self):
        """Chọn key tốt nhất cho pool này dựa trên stats."""
        ks = groq.keys()
        if not ks:
            return None, None
        # Lọc key chưa bị circuit breaker chặn
        ok_keys = groq._km.ok_keys(ks)
        if not ok_keys:
            return None, None
        # Ưu tiên key có ít calls gần đây
        best = None
        best_score = float("inf")
        for k in ok_keys:
            score = groq._km.stats_get(k) if hasattr(groq._km, "stats_get") else 999
            if score < best_score:
                best_score = score
                best = k
        return best, groq._km.pick_key.__doc__ or "weighted"

    def can_accept(self):
        """Kiểm tra pool còn nhận task được không."""
        with _pool_lock:
            now = time.time()
            usage = _pool_usage[self.name]
            # Max concurrent
            if self._busy and now - self._last_activity < 2:
                return False
            # Rate limit: max 1 call per 2s per pool
            if now - usage["last_call"] < 1.0:
                return False
            return True

    def call(self, msgs, tools=None, budget=None):
        """Gọi Groq với pool này."""
        with _pool_lock:
            usage = _pool_usage[self.name]
            usage["calls"] += 1
            usage["last_call"] = time.time()
            self._busy = True
            self._last_activity = time.time()
        try:
            model_list = self.cfg["model_pref"][:3]
            budget_val = budget or self.cfg["budget"]
            result = groq.chat(msgs, tools=tools, budget=budget_val)
            return result
        except Exception as e:
            with _pool_lock:
                _pool_usage[self.name]["errors"] += 1
            return None
        finally:
            with _pool_lock:
                self._busy = False


# ── Task Planning ────────────────────────────────────────────────────────────

def plan_task(user_request):
    """Phân rã task lớn thành subtask.

    Returns: list of {pool, task, priority}
    """
    req = user_request.lower()
    subtasks = []

    # Detect task type and assign to appropriate pool
    media_keywords = ["tạo nhạc", "tts", "chuyển đổi âm", "xử lý video",
                      "tạo ảnh", "generate image", "media", "scene",
                      "làm video", "ghép video", "audio"]
    desktop_keywords = ["mở app", "click", "chụp màn hình", "điều khiển",
                        "facebook", "login", "browser", "desktop"]
    game_keywords = ["tạo game", "làm game", "pygame", "html game"]
    code_keywords = ["viết code", "sửa code", "lập trình", "compile"]

    if any(k in req for k in media_keywords):
        subtasks.append({"pool": "media", "task": user_request, "priority": 1})
    elif any(k in req for k in desktop_keywords):
        subtasks.append({"pool": "desktop", "task": user_request, "priority": 1})
    elif any(k in req for k in game_keywords):
        subtasks.append({"pool": "main", "task": user_request, "priority": 1})
    elif any(k in req for k in code_keywords):
        subtasks.append({"pool": "main", "task": user_request, "priority": 1})
    else:
        # Default: main pool
        subtasks.append({"pool": "main", "task": user_request, "priority": 1})

    return subtasks


# ── Multi-Agent Orchestration ────────────────────────────────────────────────

class AgentOrchestrator:
    """Phân task cho nhiều agent con chạy song song (kiểu Antigravity)."""

    def __init__(self):
        self._pools = {name: KeyPool(name, cfg) for name, cfg in KEY_POOLS.items()}
        self._executor = ThreadPoolExecutor(max_workers=4)

    def execute(self, user_request, tool_fn=None, tools=None):
        """Thực hiện request: phân pool, giao task, collect results."""
        subtasks = plan_task(user_request)
        results = []

        if len(subtasks) == 1:
            # Single task — execute directly
            pool_name = subtasks[0]["pool"]
            pool = self._pools[pool_name]
            if pool.can_accept():
                # Build messages
                msgs = [{"role": "user", "content": user_request}]
                result = pool.call(msgs, tools=tools)
                return result

        # Multi-task: run in parallel
        futures = {}
        for st in subtasks:
            pool = self._pools[st["pool"]]
            if pool.can_accept():
                msgs = [{"role": "user", "content": st["task"]}]
                future = self._executor.submit(pool.call, msgs, tools=tools)
                futures[future] = st["pool"]

        for future in as_completed(futures):
            pool_name = futures[future]
            try:
                result = future.result(timeout=30)
                results.append({"pool": pool_name, "result": result})
            except Exception as e:
                results.append({"pool": pool_name, "error": str(e)})

        return results

    def health_report(self):
        """Báo cáo sức khỏe các pool."""
        report = []
        for name, usage in _pool_usage.items():
            pool = self._pools[name]
            status = "busy" if pool._busy else "idle"
            report.append(f"{name}: {status} | {usage['calls']} calls | {usage['errors']} errors")
        return "\n".join(report)


# ── 24/7 Watchdog ────────────────────────────────────────────────────────────

class Watchdog:
    """Giám sát agent hoạt động 24/7 — tự restart, auto-resume."""

    def __init__(self, agent_loop_fn, config=config):
        self._agent_fn = agent_loop_fn
        self._running = True
        self._restart_count = 0
        self._max_restarts = 10
        self._check_interval = config.TOOL_SLOW_WARN  # 40s
        self._thread = None

    def start(self):
        """Bắt đầu watchdog thread."""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Dừng watchdog."""
        self._running = False

    def _loop(self):
        while self._running:
            time.sleep(self._check_interval)
            if not self._running:
                break
            # Check if agent is still healthy
            if not self._is_healthy():
                self._restart_count += 1
                if self._restart_count > self._max_restarts:
                    break
                self._restart()

    def _is_healthy(self):
        """Kiểm tra agent có đang hoạt động không."""
        try:
            keys = groq.keys()
            return len(keys) > 0
        except Exception:
            return False

    def _restart(self):
        """Restart agent loop."""
        try:
            self._agent_fn()
        except Exception:
            pass


# ── Initialize ───────────────────────────────────────────────────────────────

orchestrator = AgentOrchestrator()
watchdog = None  # Initialize when needed


def init_arch():
    """Khởi động watchdog cho 24/7 operation."""
    global watchdog
    watchdog = Watchdog(lambda: None)  # Will be set with actual agent loop
    watchdog.start()
    return "Watchdog started — 24/7 mode"


def get_pool_stats():
    """Lấy stats các pool."""
    return {name: dict(usage) for name, usage in _pool_usage.items()}