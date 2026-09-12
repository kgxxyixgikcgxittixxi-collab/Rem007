import os, re, sqlite3, threading, time, json
import requests

from config import DIR, MODEL_PREF_CHAT, MODEL_PREF_CLONE, MODEL_PREF_VISION, MODEL_PREF_FB

DB = os.path.join(DIR, "rem.db")
SECRET = os.path.join(DIR, ".secret")
_DB_LOCK = threading.Lock()

# Một kết nối dùng chung giữa thread UI (/key...) và thread worker (agent task).
# Bắt buộc mọi truy cập DB đi qua _DB_LOCK, nếu không sqlite3 sẽ báo
# "Recursive use of cursors not allowed" / "database is locked" → /key thất bại.
_conn = sqlite3.connect(DB, check_same_thread=False)
_cur = _conn.cursor()
_cur.execute("CREATE TABLE IF NOT EXISTS gq(key TEXT UNIQUE)")
_conn.commit()

# ── mã hoá key lưu trong rem.db ──
# Khoá bí mật (~/.rem_ai/.secret, 0600) nằm ngoài repo, tự sinh nếu chưa có.
# Ai lấy được file DB cũng chỉ thấy chuỗi mã hoá, không đọc được key gốc.
# Hai chế độ mã hoá, tự chọn theo thư viện có sẵn (đều lưu tiền tố để đọc lại được):
#   "f1:" → Fernet   (khi có package cryptography)
#   "s1:" → PBKDF2-SHA256 XOR stream  (thuần stdlib — không cần cài thêm)
import base64, hashlib
_PREFER_FERNET = None


def _has_fernet():
    global _PREFER_FERNET
    if _PREFER_FERNET is None:
        try:
            __import__("cryptography.fernet")
            _PREFER_FERNET = True
        except Exception:
            _PREFER_FERNET = False
    return _PREFER_FERNET


def _secret_bytes():
    """32 byte khoá bí mật. Nhận diện cả file cũ dạng Fernet key (b64 44) lẫn raw 32B."""
    try:
        raw = open(SECRET, "rb").read().strip()
    except Exception:
        raw = b""
    if raw:
        if len(raw) == 32:
            return raw
        try:
            b = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
        except Exception:
            b = b""
        if len(b) == 32:
            return b
    secret = os.urandom(32)
    fd = os.open(SECRET, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(secret)
    try:
        os.chmod(SECRET, 0o600)
    except Exception:
        pass
    return secret


def _fernet_key():
    return base64.urlsafe_b64encode(_secret_bytes())


def _fernet_enc(k):
    from cryptography.fernet import Fernet
    return "f1:" + Fernet(_fernet_key()).encrypt(k.encode()).decode()


_FF = None


def _fernet_dec(t):
    global _FF
    if _FF is None:
        from cryptography.fernet import Fernet
        _FF = Fernet(_fernet_key())
    return _FF.decrypt(t.encode()).decode()


_STD_ITER = 30_000


def _xor_stream(secret, salt, iv, data, iterations=_STD_ITER):
    master = hashlib.pbkdf2_hmac("sha256", secret, salt, iterations, dklen=32)
    out = bytearray()
    ctr = 0
    while len(out) < len(data):
        out += hashlib.sha256(master + iv + ctr.to_bytes(4, "big")).digest()
        ctr += 1
    ks = bytes(out[:len(data)])
    return bytes(a ^ b for a, b in zip(data, ks))


def _enc_std(k):
    secret = _secret_bytes()
    salt, iv = os.urandom(16), os.urandom(16)
    ct = _xor_stream(secret, salt, iv, k.encode())
    # nhúng cả số vòng lặp → nâng/hạ iterations sau này vẫn đọc lại được dữ liệu cũ
    return f"s1:{_STD_ITER:06x}:" + iv.hex() + ":" + salt.hex() + ":" + ct.hex()


def _dec_std(t):
    try:
        parts = t.split(":")
        if len(parts) == 4:  # định dạng cũ: s1:iv:salt:ct
            _, ivh, salt, cth = parts
            iterations = _STD_ITER
        else:                # định dạng mới: s1:iter:iv:salt:ct
            _, _i, ivh, salt, cth = parts
            iterations = int(_i, 16)
        ct = bytes.fromhex(cth)
        return _xor_stream(_secret_bytes(), bytes.fromhex(salt), bytes.fromhex(ivh), ct, iterations).decode()
    except Exception:
        return None


def _enc(k):
    if _has_fernet():
        try:
            return _fernet_enc(k)
        except Exception:
            pass
    return _enc_std(k)


def _dec(t):
    if not t:
        return None
    if t.startswith("f1:"):
        if not _has_fernet():
            return None
        try:
            return _fernet_dec(t[3:])
        except Exception:
            return None
    if t.startswith("s1:"):
        return _dec_std(t)
    return None


_KCACHE = {"t": 0.0, "v": []}


def keys():
    """Danh sách key đã giải mã. Cache 10s để tránh chạy PBKDF2/Fernet lại từng lần."""
    now = time.time()
    with _DB_LOCK:
        if _KCACHE["v"] and now - _KCACHE["t"] < 10:
            return list(_KCACHE["v"])
        rows = _cur.execute("SELECT key FROM gq").fetchall()
    out = []
    for (k,) in rows:
        d = _dec(k)
        if d is not None:
            out.append(d)
        elif re.match(r"^gsk_", k or ""):
            # key cũ còn lưu dạng rõ → mã hoá lại (migration một lần)
            e = _enc(k)
            with _DB_LOCK:
                _cur.execute("UPDATE gq SET key=? WHERE key=?", (e, k))
                _conn.commit()
            out.append(k)
    with _DB_LOCK:
        _KCACHE["t"] = now
        _KCACHE["v"] = list(out)
    return out


def add_key(k):
    k = (k or "").strip()
    if not re.match(r"^gsk_[A-Za-z0-9]{20,}$", k):
        return False
    with _DB_LOCK:
        try:
            # dedupe theo key GỐC (không theo chuỗi mã hoá — mã hoá có salt ngẫu nhiên)
            rows = _cur.execute("SELECT key FROM gq").fetchall()
            for (stored,) in rows:
                if _dec(stored) == k:
                    return True
            _cur.execute("INSERT INTO gq(key) VALUES(?)", (_enc(k),))
            _conn.commit()
        except Exception:
            return False
    with _DB_LOCK:
        _KCACHE["t"] = 0.0  # invalidate cache
    return True


def scan(t):
    return sum(1 for k in re.findall(r"gsk_[A-Za-z0-9]{40,}", t) if add_key(k))


def seed_defaults():
    """Nạp key mặc định từ biến môi trường REM_GQ_SEED (phân cách xuống dòng/phẩy/dấu cách)
    chỉ khi DB chưa có key nào. Key được mã hoá trước khi lưu, không nằm trong repo."""
    if keys():
        return 0
    txt = os.environ.get("REM_GQ_SEED", "") or ""
    found = re.findall(r"gsk_[A-Za-z0-9]{40,}", txt)
    return sum(1 for k in found if add_key(k))


_MODELS = {"items": None, "at": 0.0}
_MODEL_URL = "https://api.groq.com/openai/v1/models"


def models(force=False):
    """Trả dict các model khả dụng từ API Groq, cache 5 phút."""
    now = time.time()
    if (_MODELS["items"] is not None and not force and now - _MODELS["at"] < 300) or not keys():
        _MODELS["items"] = _MODELS["items"] or {}
        return _MODELS["items"]
    ks = keys()
    for k in ks[:3]:
        try:
            r = requests.get(_MODEL_URL, headers={"Authorization": f"Bearer {k}"}, timeout=12)
            if r.status_code == 200:
                _MODELS["items"] = {m["id"] for m in r.json().get("data", [])}
                _MODELS["at"] = now
                return _MODELS["items"]
        except Exception:
            continue
    return _MODELS["items"] or {}


def resolve(prefs):
    """Trả danh sách model tồn tại, theo thứ tự ưu tiên prefs; fallback model bất kỳ."""
    ms = models()
    if not ms:
        return list(prefs)
    ordered = [m for m in prefs if m in ms]
    if not ordered:
        ordered = [next(iter(sorted(ms)), prefs[0])]
    return ordered or list(prefs)


def chat_models():
    return resolve(MODEL_PREF_CHAT)


def clone_models():
    return resolve(MODEL_PREF_CLONE)


def vision_models():
    return resolve(MODEL_PREF_VISION)


def fb_models():
    return resolve(MODEL_PREF_FB)


_RATE = (413, 429)
_NET_ERRS = ("ReadTimeout", "ConnectTimeout", "ConnectionError", "ReadError",
             "RemoteDisconnected", "ChunkedEncodingError", "ProxyError")
# Lỗi 5xx/408: server quá tải → backoff ngắn + jitter rồi thử tiếp (không đốt cooldown)
_SERVER_ERRS = (500, 502, 503, 504, 408)

_STATS_FILE = os.path.join(DIR, "key_stats.json")
_STATS_SAVE_EVERY = 20      # lưu stats sau mỗi N sự kiện báo cáo (tránh ghi nhiều)


def _parse_retry_after(v):
    """Retry-After có thể là giây hoặc HTTP-date → trả về số giây chờ, else None."""
    if not v:
        return None
    v = str(v).strip()
    if v.isdigit():
        return int(v)
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(v)
        return max(0, int(d.timestamp() - time.time()))
    except Exception:
        return None


class KeyManager:
    """Quản lý nhiều Groq key (mỗi key 1 tài khoản độc lập) với:
    - Token bucket per key (RPM → chủ động tiết lưu, tránh 429 ngay từ đầu)
    - Circuit breaker cho key hỏng/auth lỗi
    - Cooldown theo Retry-After, leo thang khi 429 liên tiếp (quota cửa sổ cạn)
    - Backoff + jitter cho lỗi 5xx/mạng
    - Stats lưu file → học được key nào tốt, sống sót qua restart
    Áp dụng các pattern: liteLLM weighted routing, token-bucket (pierringshot/groq-api),
    circuit breaker (resilience4j)."""

    def __init__(self):
        self._rr = 0
        self._stats = {}        # key -> dict thống kê
        self._rpm_hist = {}     # key -> deque timestamps request (token bucket)
        self._lock = threading.Lock()
        self._dirty = 0
        self._CIRCUIT_THRESHOLD = 5      # lỗi liên tiếp trước khi mở circuit
        self._CIRCUIT_RECOVERY = 90      # giây trước khi half-open
        self._COOLDOWN_BASE = 30         # cooldown tối thiểu khi 429
        self._COOLDOWN_DEFAULT = 50      # khi không có Retry-After
        self._COOLDOWN_SCALE = 60        # leo thang thêm mỗi đợt 429 liên tiếp
        self._COOLDOWN_MAX = 900         # trần cooldown (15 phút)
        self._RPM_SAFE = 26              # token bucket: tối đa yêu cầu/phút mỗi key (Groq ~30)
        self._RPM_WINDOW = 60.0
        self._AUTH_COOLDOWN = 3600       # key 401/403 → đóng 1 giờ
        # key bị cạn quota cửa sổ (429 nhiều liên tiếp) → cooldown dài (gần như nghỉ hôm nay)
        self._EXHAUST_COOLDOWN = 1800

    def _get_stats(self, key):
        s = self._stats.setdefault(key, {
            "success": 0, "fail": 0, "rate": 0, "net": 0,
            "last_ok": 0.0, "last_fail": 0.0,
            "cooldown_until": 0.0, "cooldown_reason": "",
            "circuit_failures": 0, "circuit_open": False,
            "rate_streak": 0, "auth_fails": 0, "auth_dead": False,
        })
        return s

    def _get_hist(self, key):
        import collections
        if key not in self._rpm_hist:
            self._rpm_hist[key] = collections.deque()
        return self._rpm_hist[key]

    def _save(self):
        self._dirty += 1
        if self._dirty % _STATS_SAVE_EVERY != 0:
            return
        try:
            payload = {k: dict(v) for k, v in self._stats.items()}
            tmp = _STATS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, _STATS_FILE)
        except Exception:
            pass

    def load(self):
        try:
            with open(_STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._stats = {k: dict(v) for k, v in (data or {}).items()}
            # quá trình restart → cooldown cũ không còn giá trị, thả lại
            now = time.time()
            for s in self._stats.values():
                if s.get("cooldown_until", 0) - now > 120:
                    s["cooldown_until"] = now
        except Exception:
            pass

    def _tokens_available(self, key, now):
        """Token bucket: chủ động giới hạn RPM → không tới mức 429 mới biết."""
        h = self._get_hist(key)
        while h and now - h[0] > self._RPM_WINDOW:
            h.popleft()
        return len(h) < self._RPM_SAFE

    def pick_key(self, available_keys):
        """Chọn key bằng round-robin trong nhóm key KHỎE (đủ RPM, không cooldown/circuit,
        không vừa fail). Không để 1 key 'hot' gánh hết — load chia đều để né rate limit."""
        now = time.time()
        with self._lock:
            healthy = []
            for i in range(len(available_keys)):
                k = available_keys[(self._rr + i) % len(available_keys)]
                s = self._get_stats(k)
                if s.get("auth_dead"):
                    continue  # key chết xác thực (401/403 lặp) → cách ly, không chọn
                if s["circuit_open"]:
                    if now - s["last_fail"] < self._CIRCUIT_RECOVERY:
                        continue
                    s["circuit_open"] = False
                    s["circuit_failures"] = max(0, s["circuit_failures"] - 2)
                if s["cooldown_until"] > now:
                    continue
                if not self._tokens_available(k, now):
                    continue
                # vừa fail gần đây (30s) → cho hồi phục trước khi xoay lại
                if now - s["last_fail"] < 30:
                    continue
                healthy.append(k)
            if healthy:
                choice = healthy[0]
                self._rr = (self._rr + 1) % len(available_keys)
                return choice, 0
            # không key khỏe → chọn key có cooldown ngắn nhất (ưu tiên key không chết auth;
            # chỉ dùng key auth_dead khi toàn bộ pool đều chết)
            best_wait = float("inf")
            best_key = None
            dead_wait = float("inf")
            dead_key = None
            for k in available_keys:
                s = self._get_stats(k)
                if s["circuit_open"] and now - s["last_fail"] < self._CIRCUIT_RECOVERY:
                    continue
                wait = max(0, s["cooldown_until"] - now)
                if s.get("auth_dead"):
                    if wait < dead_wait:
                        dead_wait = wait
                        dead_key = k
                elif wait < best_wait:
                    best_wait = wait
                    best_key = k
            if best_key is None:
                best_key, best_wait = dead_key, dead_wait
            if best_key is None:
                best_score = float("inf")
                for k in available_keys:
                    n = len(self._get_hist(k))
                    if n < best_score:
                        best_score = n
                        best_key = k
            return best_key, best_wait

    def report_success(self, key):
        with self._lock:
            s = self._get_stats(key)
            s["success"] += 1
            s["last_ok"] = time.time()
            s["circuit_failures"] = max(0, s["circuit_failures"] - 1)
            s["rate_streak"] = 0
            s["cooldown_reason"] = ""
            # key vừa thành công → cooldown cũ đã lỗi thời, mở khóa ngay
            # (trước đây success không xóa cooldown_until nên key khỏe vẫn bị cấm oan)
            s["cooldown_until"] = 0.0
            s["auth_dead"] = False
            s["auth_fails"] = 0
            now = time.time()
            h = self._get_hist(key)
            h.append(now)          # token-bucket ghi nhận request thành công
            while h and now - h[0] > self._RPM_WINDOW:
                h.popleft()
        self._save()

    def report_rate_limit(self, key, retry_after=None):
        with self._lock:
            s = self._get_stats(key)
            s["rate"] += 1
            s["rate_streak"] += 1
            s["last_fail"] = time.time()
            ra = _parse_retry_after(retry_after)
            base = ra if ra else self._COOLDOWN_DEFAULT
            base = max(self._COOLDOWN_BASE, base)
            # leo thang: 429 liên tiếp → cooldown mỗi lần thêm (quota cửa sổ gần cạn)
            if s["rate_streak"] >= 3:
                s["cooldown_until"] = time.time() + self._EXHAUST_COOLDOWN
                s["cooldown_reason"] = "exhausted"
                s["rate_streak"] = 0
            elif s["rate_streak"] >= 2:
                s["cooldown_until"] = time.time() + min(base + s["rate_streak"] * self._COOLDOWN_SCALE, self._COOLDOWN_MAX)
                s["cooldown_reason"] = f"rate streak={s['rate_streak']}"
            else:
                # 429 lẻ tẻ (streak 1) → chỉ nghỉ ngắn, tránh cấm oan cả phút
                # (trước đây 1 phát 429 đã cooldown 50-110s, cả pool cùng dính)
                s["cooldown_until"] = time.time() + (ra if ra else 10.0)
                s["cooldown_reason"] = "rate 1 lần"
        self._save()

    def report_failure(self, key, is_network=False, is_server=False):
        with self._lock:
            s = self._get_stats(key)
            s["fail"] += 1
            s["last_fail"] = time.time()
            s["circuit_failures"] += 1
            if is_network:
                s["net"] += 1
            if s["circuit_failures"] >= self._CIRCUIT_THRESHOLD:
                s["circuit_open"] = True
                s["cooldown_reason"] = "circuit_open"
        self._save()

    def report_auth_fail(self, key):
        with self._lock:
            s = self._get_stats(key)
            s["circuit_open"] = True
            s["last_fail"] = time.time()
            s["cooldown_until"] = time.time() + self._AUTH_COOLDOWN
            s["cooldown_reason"] = "auth"
            # 401/403 lặp ≥2 lần → key chết thật (sai key/hết quyền) → cách ly khỏi vòng xoay
            # (report_success sẽ gỡ cách ly nếu key sống lại)
            s["auth_fails"] = s.get("auth_fails", 0) + 1
            if s["auth_fails"] >= 2:
                s["auth_dead"] = True
        self._save()

    def all_cooldown(self, available_keys):
        now = time.time()
        with self._lock:
            return all(self._get_stats(k)["cooldown_until"] > now for k in available_keys) if available_keys else False

    def stats_summary(self):
        now = time.time()
        with self._lock:
            lines = []
            for k, s in sorted(self._stats.items(), key=lambda x: -x[1]["success"]):
                status = "DEAD" if s.get("auth_dead") else ("OPEN" if s["circuit_open"] else ("CD" if s["cooldown_until"] > now else "OK"))
                r = s.get("cooldown_reason") or ""
                lines.append(f"  {k[:14]}... {status:3} ok={s['success']} fail={s['fail']} "
                             f"rate={s['rate']} net={s['net']}" + (f" [{r}]" if r else ""))
            return "\n".join(lines)

    def ok_keys(self, available_keys):
        """Số key đang dùng được ngay (không cooldown, không circuit)."""
        now = time.time()
        with self._lock:
            return sum(1 for k in available_keys
                       if not self._get_stats(k)["circuit_open"] and self._get_stats(k)["cooldown_until"] <= now)


_km = KeyManager()
_km.load()

# ── Org-level rate-limit tracker ──────────────────────────────────────
# Groq giới hạn theo organization, không phải theo key: khi nhiều key liên
# tiếp cùng trả 429 thì chờ 1 lần theo Retry-After thay vì hammer từng key.
# Tiến trình làm việc KHÔNG mất: lịch sử tool đã append vào sessions DB ngay
# sau mỗi tool, còn retry LLM chỉ giữ nguyên msgs và thử lại request đó.
_ORG = {"until": 0.0, "retry_after": 0, "hits": 0}
_ORG_LOCK = threading.Lock()


def rate_wait_remaining():
    """Số giây còn phải chờ do rate-limit org-level (0 = không phải chờ)."""
    with _ORG_LOCK:
        return max(0.0, _ORG["until"] - time.time())


def _note_org_rate(retry_after=None):
    """Ghi nhận 1 hit 429 org-level, trả về số giây nên chờ."""
    with _ORG_LOCK:
        ra = _parse_retry_after(retry_after)
        wait = ra if ra else 8
        wait = max(2, min(wait, 60))
        now = time.time()
        _ORG["hits"] += 1
        _ORG["retry_after"] = wait
        _ORG["until"] = max(_ORG["until"], now + wait)
        return wait


def _post(body, model, timeout=30, budget=None):
    """Gọi Groq với KeyManager: weighted key selection, circuit breaker, smart retry."""
    body = dict(body)
    effort = os.environ.get("REM_REASONING", "low").strip().lower()
    if model.startswith("openai/gpt-oss") and effort in ("low", "medium", "high"):
        body["reasoning_effort"] = effort
    ks = keys()
    if not ks:
        return None
    last = None
    attempts = 0
    rate_hits = 0
    max_attempts = min(len(ks) + 4, 16)  # xoay key nhưng chặn hammer khi 429 org-level
    end = time.time() + (budget if budget and budget > 0 else 75)
    import random as _rd
    while time.time() < end and attempts < max_attempts:
        # Org-level 429 đang active → chờ 1 lần duy nhất, không thử từng key vô ích
        rem = rate_wait_remaining()
        if rem > 1:
            time.sleep(min(rem, max(0.5, end - time.time()), 15))
            continue
        attempts += 1
        choice, wait = _km.pick_key(ks)
        if choice is None:
            break
        if wait > 0:
            time.sleep(min(wait, max(0.5, end - time.time())))
            continue
        to = max(1, min(timeout, int(end - time.time() + 1)))
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {choice}"},
                json={"model": model, **body}, timeout=to,
            )
            if r.status_code == 200:
                _km.report_success(choice)
                return r
            if r.status_code in _RATE:
                retry = r.headers.get("retry-after")
                _km.report_rate_limit(choice, retry)
                _note_org_rate(retry)
                rate_hits += 1
                last = "RATE"
                # Nhiều key liên tiếp cùng 429 = giới hạn org → ngủ 1 lần rồi thử tiếp
                if rate_hits >= 3:
                    rem = rate_wait_remaining()
                    if rem > 1:
                        time.sleep(min(rem, max(0.5, end - time.time()), 20))
                    rate_hits = 0
            elif r.status_code in _SERVER_ERRS:
                # server quá tải → chờ ngắn + jitter rồi thử key khác (không chặn key)
                wait = min(2 ** min(attempts, 4), 8) * _rd.uniform(0.7, 1.3)
                time.sleep(min(wait, max(0.2, end - time.time())))
                _km.report_failure(choice, is_server=True)
                last = f"HTTP {r.status_code}"
                continue
            else:
                last = f"HTTP {r.status_code}"
                if r.status_code in (401, 403):
                    _km.report_auth_fail(choice)
                    break
                _km.report_failure(choice)
        except Exception as e:
            last = type(e).__name__
            _km.report_failure(choice, is_network=True)
            if last in _NET_ERRS:
                time.sleep(0.5 * _rd.uniform(0.7, 1.3))  # brief jittered backoff
    if last == "RATE":
        return "RATE"
    if last and (last not in _SERVER_ERRS):
        print(f"[groq] that bai ({model}): {last}", file=__import__("sys").stderr)
    return None


def chat(msgs, tools=None, budget=None):
    """Trả về message của model đầu tiên trả lời được trong khung thời gian budget."""
    body = {"messages": msgs, "max_tokens": 16384}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    chain = chat_models() + [m for m in fb_models() if m not in chat_models()]
    end = time.time() + (budget if budget and budget > 0 else 75)
    for attempt in range(3):
        if time.time() >= end:
            return None
        all_rate = True
        for m in chain[:5]:
            if time.time() >= end:
                break
            r = _post(body, m, budget=max(1, end - time.time()))
            if r == "RATE":
                continue
            if r is not None:
                return r.json()["choices"][0]["message"]
            all_rate = False
        if all_rate:
            wait = min(1 + attempt, 4)
            time.sleep(max(0.0, min(wait, end - time.time())))
            continue
        return None
    return None


def _parse_xml_tools(content):
    """Fallback: một số model (qwen3.8-27b) đôi khi trả tool_call dạng XML
    <function_calls><invoke name=".."><parameter name="..">..</parameter></invoke></function_calls>
    thay vì JSON tool_calls chuẩn của OpenAI. Parse XML → danh sách tool_calls."""
    if not content or "<function_calls>" not in content:
        return []
    calls = []
    m = re.search(r"<function_calls>(.*?)</function_calls>", content, re.S)
    if not m:
        return []
    for inv in re.finditer(r'<invoke\s+name="([^"]+)">(.*?)</invoke>', m.group(1), re.S):
        name, body = inv.group(1), inv.group(2)
        params = {}
        for p in re.finditer(r'<parameter\s+name="([^"]+)">(.*?)</parameter>', body, re.S):
            k, v = p.group(1), p.group(2)
            v = v.strip()
            try:
                params[k] = json.loads(v)
            except Exception:
                params[k] = v
        if name and params:
            calls.append({"id": f"fc-{len(calls)}", "type": "function",
                          "function": {"name": name, "arguments": json.dumps(params, ensure_ascii=False)}})
    return calls


def _iter_stream(r, on_delta):
    """Tiêu thụ response stream Groq → message cuối. on_delta(ev) nhận
    {"type":"content"|"thinking","text":...} để UI hiện tiến độ chữ trực tiếp."""
    acc = {"content": "", "thinking": "", "tc": []}

    def emit(kind, s):
        if on_delta and s:
            try:
                on_delta({"type": kind, "text": s})
            except Exception:
                pass

    try:
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            data = line[5:].strip() if line.startswith("data:") else line.strip()
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                d = json.loads(data)
            except Exception:
                continue
            ch = (d.get("choices") or [{}])[0]
            delta = ch.get("delta") or {}
            if delta.get("reasoning_content"):
                acc["thinking"] += delta["reasoning_content"]
                emit("thinking", delta["reasoning_content"])
            if delta.get("content"):
                acc["content"] += delta["content"]
                emit("content", delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                while len(acc["tc"]) <= idx:
                    acc["tc"].append({"id": "", "name": "", "args": ""})
                fn = tc.get("function") or {}
                acc["tc"][idx]["id"] += tc.get("id") or ""
                acc["tc"][idx]["name"] += fn.get("name") or ""
                acc["tc"][idx]["args"] += fn.get("arguments") or ""
    except Exception:
        return None
    finally:
        try:
            r.close()
        except Exception:
            pass
    tc = []
    for frag in acc["tc"]:
        try:
            args = json.loads(frag["args"] or "{}")
        except Exception:
            args = {}
        tc.append({"id": frag["id"], "type": "function",
                   "function": {"name": frag["name"], "arguments": json.dumps(args, ensure_ascii=False)}})
    msg = {"role": "assistant"}
    if acc["content"]:
        msg["content"] = acc["content"]
    if acc["thinking"]:
        msg["reasoning_content"] = acc["thinking"]
    if not tc:
        # fallback: parse XML <function_calls> từ CẢ content lẫn thinking
        # (một số model qwen để XML tool call trong reasoning_content)
        tc = _parse_xml_tools(acc["content"]) or _parse_xml_tools(acc["thinking"])
        if tc:
            body = re.sub(r"<function_calls>.*?</function_calls>", "", acc["content"], flags=re.S).strip()
            if body:
                msg["content"] = body
            else:
                msg.pop("content", None)
    if tc:
        msg["tool_calls"] = tc
    if msg.get("content") or msg.get("reasoning_content") or tc:
        return msg
    return None


def chat_stream(msgs, tools=None, budget=None, on_delta=None):
    """Gọi Groq dạng STREAM — UI thấy chữ/tiến độ đang chảy, không bị 'treo im'.
    Trả message cuối giống chat(), kèm on_delta để cập nhật tiến độ.
    max_tokens 4096 (~5KB/lần): mỗi phản hồi nhỏ → chạy hết trong timeout, KHÔNG bị cắt
    giữa stream như các phản hồi cồng kềnh; file lớn model tự ghi thành nhiều phần nhỏ."""
    body = {"messages": msgs, "max_tokens": 4096, "stream": True}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    chain = chat_models() + [m for m in fb_models() if m not in chat_models()]
    end = time.time() + (budget if budget and budget > 0 else 75)
    for attempt in range(3):
        if time.time() >= end:
            return None
        all_rate = True
        rate_models = 0
        for m in chain[:5]:
            if time.time() >= end:
                break
            r = _post(body, m, budget=max(1, end - time.time()), timeout=70)
            if r == "RATE":
                rate_models += 1
                # Đã 2 model cùng 429 = giới hạn org → chờ 1 lần, khỏi thử 3 model còn lại vô ích
                if rate_models >= 2:
                    rem = rate_wait_remaining()
                    if rem > 2 and time.time() < end:
                        time.sleep(min(rem, max(0.5, end - time.time()), 12))
                continue
            if r is None:
                all_rate = False
                continue
            all_rate = False
            return _iter_stream(r, on_delta)
        if all_rate:
            wait = min(1 + attempt, 4)  # giam backoff: 1s, 2s, 3s
            time.sleep(max(0.0, min(wait, end - time.time())))
            continue
    return None


def text(p, max_tokens=700, temp=0.2, budget=None):
    end = time.time() + (budget if budget and budget > 0 else 75)
    for m in clone_models()[:3]:
        if time.time() >= end:
            break
        r = _post({"messages": [{"role": "user", "content": p}], "max_tokens": max_tokens, "temperature": temp}, m, 20, budget=max(1, end - time.time()))
        if r is not None and r != "RATE" and r.status_code == 200:
            content = r.json()["choices"][0]["message"].get("content")
            if content:
                return content
            # content rỗng → thử model kế tiếp
            continue
    return ""


def vision(q, img, budget=None):
    try:
        d = __import__("base64").b64encode(open(img, "rb").read()).decode()
    except Exception:
        return None
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": q},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{d}"}}]}], "max_tokens": 300}
    end = time.time() + (budget if budget and budget > 0 else 75)
    for m in vision_models()[:3]:
        if time.time() >= end:
            break
        r = _post(body, m, 25, budget=max(1, end - time.time()))
        if r == "RATE":
            return None
        if r is not None and r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
    return None


_seeded = seed_defaults()
if _seeded:
    print(f"[groq] da nap {_seeded} Groq key mac dinh (da ma hoa trong DB).", file=__import__("sys").stderr)