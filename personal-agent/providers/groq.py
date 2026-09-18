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


# ── Token usage accounting (kiểu opencode: footer ↑in ↓out mỗi lượt) ──
# Groq (chuẩn OpenAI) trả "usage" trong response JSON và chunk cuối của stream
# (khi có stream_options.include_usage). Trước đây agent vứt hết → không có
# số liệu token thật. _note_usage() gom vào _UTURN (reset mỗi lượt) + _UTOT.
_UTURN = {"prompt": 0, "completion": 0, "calls": 0}
_UTOT = {"prompt": 0, "completion": 0, "calls": 0}
_ULOCK = threading.Lock()


def _note_usage(u):
    try:
        if not isinstance(u, dict):
            return
        p = int(u.get("prompt_tokens") or 0)
        c = int(u.get("completion_tokens") or 0)
        if p < 0 or c < 0:
            return
        if p == 0 and c == 0:
            return  # báo usage rỗng (vd chunk giữa stream) → không tính 1 call ảo
        with _ULOCK:
            for d in (_UTURN, _UTOT):
                d["prompt"] += p
                d["completion"] += c
                d["calls"] += 1
    except Exception:
        pass


def take_usage():
    """Lấy + reset số token của lượt vừa xong (TUI gọi sau mỗi task)."""
    with _ULOCK:
        d = dict(_UTURN)
        _UTURN.update(prompt=0, completion=0, calls=0)
    return d


def session_usage():
    """Tổng token thật từ đầu phiên (cho /stats + status bar)."""
    with _ULOCK:
        return dict(_UTOT)


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
    """Nạp key mặc định khi DB chưa có key nào, từ (1) file ~/.rem_ai/seed_keys.txt
    (mỗi key một dòng, hoặc cách nhau bởi khoảng trắng/phẩy/xuống dòng) và
    (2) biến môi trường REM_GQ_SEED. Key được mã hoá trước khi lưu vào rem.db
    và KHÔNG BAO GIỜ nằm trong repo git (file seed nằm ngoài repo)."""
    if keys():
        return 0
    txt = os.environ.get("REM_GQ_SEED", "") or ""
    try:
        with open(os.path.join(DIR, "seed_keys.txt"), "r", encoding="utf-8") as f:
            txt += "\n" + f.read()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    found = re.findall(r"gsk_[A-Za-z0-9]{20,}", txt)
    seen = set()
    uniq = [k for k in found if not (k in seen or seen.add(k))]
    return sum(1 for k in uniq if add_key(k))


_MODELS = {"items": None, "at": 0.0}
_MODEL_URL = "https://api.groq.com/openai/v1/models"

# Groq KHÔNG bị chặn ở VN → đi thẳng, KHÔNG qua proxy xray 127.0.0.1:1090.
# (xray restart ~10s là mọi request Groq qua proxy đều ProxyError — log 13/09 10:06.)
# requests với proxies={http:None,https:None} sẽ bỏ qua env HTTP(S)_PROXY.
_DIRECT = {"http": None, "https": None}


def _strip_reasoning(msgs):
    """Bỏ reasoning/encrypted content khỏi message trước khi gửi Groq.
    Gồm 'reasoning_content' (LangChain mapping), 'reasoning' (field chính thức
    của Groq) và 'encrypted_content' (OpenAI Responses). Echo lại các field
    này vào request sau có thể bị Groq từ chối → lỗi 400."""
    _DROP = ("reasoning_content", "reasoning", "encrypted_content")
    out = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "assistant":
            if any(k in m for k in _DROP):
                m = {k: v for k, v in m.items() if k not in _DROP}
        out.append(m)
    return out


def models(force=False):
    """Trả dict các model khả dụng từ API Groq, cache 5 phút."""
    now = time.time()
    if (_MODELS["items"] is not None and not force and now - _MODELS["at"] < 300) or not keys():
        _MODELS["items"] = _MODELS["items"] or {}
        return _MODELS["items"]
    ks = keys()
    for k in ks[:3]:
        try:
            r = requests.get(_MODEL_URL, headers={"Authorization": f"Bearer {k}"}, timeout=12, proxies=_DIRECT)
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
    ordered = resolve(MODEL_PREF_CHAT)
    # Model yêu thích (/models <số>, kiểu opencode favorite) luôn đứng đầu.
    try:
        fav = get_favorite()
        if fav:
            ordered = [fav] + [m for m in ordered if m != fav]
    except Exception:
        pass
    return ordered


_MODEL_FILE = os.path.join(DIR, "model.json")


def get_favorite():
    """Model chat user ghim (/models <số>). Rỗng nếu chưa ghim."""
    try:
        with open(_MODEL_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        m = str(d.get("chat_model") or "").strip()
        return m
    except Exception:
        return ""


def set_favorite(model):
    """Ghim model chat (atomic). Trả True nếu lưu được."""
    model = str(model or "").strip()
    if not model:
        return False
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = _MODEL_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"chat_model": model}, f, ensure_ascii=False)
        os.replace(tmp, _MODEL_FILE)
        return True
    except Exception:
        return False


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

# Lỗi HTTP deterministic (400 context tràn, tool_use_failed...) — thử 23 key
# vô ích, không đốt budget. Ghi chi tiết để chat_stream/agent trả lỗi cho agent
# tự xử lý (vd compact context) thay vì báo "quá tải" mơ hồ.
# Dùng threading.local — trước đây là global: helper thread (compact/text song song)
# có thể ghi đè cờ khiến lượt chính trả lỗi sai.
_TLS = threading.local()


def _det_flag():
    return _TLS.__dict__.setdefault("det_err", False)


def _det_msg():
    return _TLS.__dict__.get("last_err", "")


def _set_det(err=False, msg=""):
    _TLS.__dict__["det_err"] = err
    _TLS.__dict__["last_err"] = msg


def last_error():
    """Lỗi HTTP cuối cùng (nếu gặp) — dùng để agent tự đánh giá, không spam log."""
    return _det_msg()


def _is_cancelled(cancel):
    """True nếu user đã ESC//stop (kiểu opencode session_interrupt)."""
    try:
        return bool(cancel is not None and cancel.is_set())
    except Exception:
        return False


def _sleep_cancel(secs, cancel=None, end=None):
    """Ngủ theo lát 0.2s để ESC//stop ngắt ngay (≤0.2s) thay vì treo hết timeout.
    Trả True nếu bị hủy/hết giờ (caller nên return None ngay)."""
    try:
        secs = max(0.0, min(float(secs or 0), 15.0))
    except Exception:
        return True
    t0 = time.time()
    while (time.time() - t0) < secs:
        if _is_cancelled(cancel):
            return True
        if end is not None and time.time() >= end:
            return True
        time.sleep(min(0.2, max(0.0, secs - (time.time() - t0))))
    return bool(_is_cancelled(cancel) or (end is not None and time.time() >= end))

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


def _parse_reset(v):
    """Groq reset header '7.66s'/'2m59.56s' → số giây. None nếu không parse được."""
    if not v:
        return None
    m = re.match(r"^\s*(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?\s*$", str(v))
    if not m or not any(m.groups()):
        return None
    h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mi * 60 + s


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
        self._rotations = 0      # số lần đổi key cứu lượt: 429 key A → thành công bằng key B
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
        # key bị cạn quota cửa sổ (429 nhiều liên tiếp) → cooldown vừa phải, KHÔNG 30 phút.
        # Groq miễn phí reset theo phút (RPU/RPM window) → khóa 90s là đủ chờ cửa sổ mới.
        # Xoay 23 key: key nào hết cửa sổ chỉ cần nghỉ ngắn, không phải nghỉ nguyên ngày.
        self._EXHAUST_COOLDOWN = 90
        # ── Preempt quota (đọc header Groq, né TRƯỚC khi 429) ──
        # Mỗi response đều kèm x-ratelimit-remaining-tokens (TPM còn lại) và
        # x-ratelimit-remaining-requests (RPD còn lại). Key sắp hết → bỏ qua,
        # xoay key khác ngay, không tốn 1 round-trip ăn 429.
        self._rl = {}              # key -> {ts, tpm_left, rpd_left, reset_in}
        self._TPM_FLOOR = 1500     # TPM còn lại dưới mức này → nhường key khác

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

    def pick_key(self, available_keys, exclude=None):
        """Chọn key bằng round-robin trong nhóm key KHỎE (đủ RPM, không cooldown/circuit,
        không vừa fail). Không để 1 key 'hot' gánh hết — load chia đều để né rate limit.
        exclude: tập key vừa 429 trong request hiện tại → loại hẳn, XOAY NGAY sang key khác,
        không bao giờ thử lại key vừa hết giới hạn trong cùng 1 lượt."""
        now = time.time()
        excluded = set(exclude or ())
        with self._lock:
            healthy = []
            for i in range(len(available_keys)):
                k = available_keys[(self._rr + i) % len(available_keys)]
                if k in excluded:
                    continue  # vừa 429 lượt này → đổi key khác luôn
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
                if self._rl_skip(k, now):
                    continue  # sắp hết TPM/RPD thật → nhường key khác, khỏi ăn 429
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
            # chỉ dùng key auth_dead khi toàn bộ pool đều chết).
            # Key vừa 429 lượt này (exclude) chỉ dùng khi KHÔNG còn key nào khác — hết 23 key
            # mới chờ, còn key khỏe là xoay tiếp, không chờ.
            best_wait = float("inf")
            best_key = None
            best_tpm = -1.0
            dead_wait = float("inf")
            dead_key = None
            ex_wait = float("inf")
            ex_key = None
            for k in available_keys:
                s = self._get_stats(k)
                if s["circuit_open"] and now - s["last_fail"] < self._CIRCUIT_RECOVERY:
                    continue
                wait = max(0, s["cooldown_until"] - now)
                if k in excluded:
                    if wait < ex_wait:
                        ex_wait = wait
                        ex_key = k
                    continue
                if s.get("auth_dead"):
                    if wait < dead_wait:
                        dead_wait = wait
                        dead_key = k
                elif wait < best_wait - 1e-9 or (abs(wait - best_wait) < 1e-9
                                                 and self._rl_tpm(k, now) > best_tpm):
                    best_wait = wait
                    best_key = k
                    best_tpm = self._rl_tpm(k, now)
            if best_key is None:
                best_key, best_wait = dead_key, dead_wait
            if best_key is None:
                best_key, best_wait = ex_key, ex_wait
            if best_key is None:
                best_score = float("inf")
                for k in available_keys:
                    if k in excluded:
                        continue
                    n = len(self._get_hist(k))
                    if n < best_score:
                        best_score = n
                        best_key = k
            # Không ngủ cả phút chờ 1 key: trả wait tối đa 3s, vừa đủ chờ hoán đổi —
            # 23 key nên ưu tiên XOAY sang key khác ngay hơn là chờ key đang cooldown.
            return best_key, min(best_wait, 3.0)

    def note_rotation(self):
        """Ghi nhận: cùng 1 yêu cầu gặp 429 nhưng XOAY key khác thành công."""
        with self._lock:
            self._rotations += 1

    def rotation_count(self):
        with self._lock:
            return self._rotations

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
            # Có Retry-After cụ thể (per-key/org thật) → tin server, khóa ĐÚNG thời gian đó.
            # Groq miễn phí trả Retry-After theo cửa sổ phút → khóa ngắn là hiệu quả nhất.
            if ra:
                s["cooldown_until"] = time.time() + min(max(ra, 15), 300)
                s["cooldown_reason"] = f"retry-after {ra}s"
            elif s["rate_streak"] >= 3:
                s["cooldown_until"] = time.time() + self._EXHAUST_COOLDOWN
                s["cooldown_reason"] = "exhausted"
                s["rate_streak"] = 0
            elif s["rate_streak"] >= 2:
                s["cooldown_until"] = time.time() + min(self._COOLDOWN_DEFAULT + s["rate_streak"] * self._COOLDOWN_SCALE, self._COOLDOWN_MAX)
                s["cooldown_reason"] = f"rate streak={s['rate_streak']}"
            else:
                # 429 lẻ tẻ (streak 1) → chỉ nghỉ ngắn, tránh cấm oan cả phút
                s["cooldown_until"] = time.time() + 10.0
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

    def note_headers(self, key, headers):
        """Ghi quota THẬT từ response headers Groq (mọi response — 200 lẫn 429 — đều có).
        headers: r.headers của requests (get case-insensitive)."""
        try:
            g = headers.get if hasattr(headers, "get") else (lambda n, d=None: None)
            tpm = g("x-ratelimit-remaining-tokens")
            rpd = g("x-ratelimit-remaining-requests")
            rst = _parse_reset(g("x-ratelimit-reset-tokens"))
            with self._lock:
                e = self._rl.setdefault(key, {})
                e["ts"] = time.time()
                if tpm is not None:
                    try:
                        e["tpm_left"] = int(float(tpm))
                    except Exception:
                        pass
                if rpd is not None:
                    try:
                        e["rpd_left"] = int(float(rpd))
                    except Exception:
                        pass
                if rst is not None:
                    e["reset_in"] = max(0.0, min(rst, 300.0))
        except Exception:
            pass

    def _rl_tpm(self, key, now):
        """TPM còn lại (còn tươi) — fallback hết key khỏe thì ưu tiên key còn nhiều."""
        e = self._rl.get(key)
        if not e or now - e.get("ts", 0) > 120:
            return float("inf")
        return e.get("tpm_left", float("inf"))

    def _rl_skip(self, key, now):
        """True = key sắp hết quota thật → bỏ qua, xoay key khác TRƯỚC khi 429."""
        e = self._rl.get(key)
        if not e or now - e.get("ts", 0) > 120:
            return False  # chưa có số liệu → cho dùng
        if e.get("rpd_left") is not None and e["rpd_left"] <= 0:
            return True  # hết lượt ngày → chờ reset UTC
        win = e.get("reset_in", 60.0) or 60.0
        if now - e["ts"] < max(win, 1.0):
            if e.get("tpm_left") is not None and e["tpm_left"] < self._TPM_FLOOR:
                return True
        return False

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
                q = ""
                e = self._rl.get(k)
                if e and now - e.get("ts", 0) < 120:
                    if e.get("tpm_left") is not None:
                        q += f" T{e['tpm_left']}"
                    if e.get("rpd_left") is not None:
                        q += f" R{e['rpd_left']}"
                lines.append(f"  {k[:14]}... {status:3} ok={s['success']} fail={s['fail']} "
                             f"rate={s['rate']} net={s['net']}" + (f" [{r}]" if r else "") + q)
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


def _post_cancelable(url, headers, payload, timeout, cancel=None, stream=False, end=None):
    """POST chặn ≤0.5s/lát để ESC ngắt ngay (kiểu opencode hard abort).
    requests.post chặn nguyên khối tới `timeout` giây nên chạy trong daemon thread,
    luồng chính poll cancel mỗi 0.2s. Hủy → bỏ kết quả nền, return (None, True).
    timeout có thể là số (giây) hoặc tuple (connect, read) — stream luôn dùng tuple ngắn.
    end (mốc tuyệt đối) → không bao giờ chờ quá mốc đó (fail-fast khi Groq chậm)."""
    box = {}

    def _do():
        try:
            box["r"] = requests.post(
                url, headers=headers, json=payload,
                timeout=timeout, proxies=_DIRECT, stream=stream,
            )
        except Exception as e:
            box["e"] = e

    th = threading.Thread(target=_do, daemon=True)
    th.start()
    # tổng chờ tối đa = read-timeout + đệm (tránh thread kẹt treo luôn lượt)
    try:
        tmax = (timeout[1] if isinstance(timeout, (list, tuple)) else timeout) + 5
    except Exception:
        tmax = 40
    if end is not None:
        tmax = min(tmax, max(0.0, end - time.time()))
    waited = 0.0
    while th.is_alive():
        if _is_cancelled(cancel):
            return None, True
        if waited >= tmax:
            return None, False
        th.join(timeout=0.2)
        waited += 0.2
    if "r" in box:
        return box["r"], False
    box_e = box.get("e")
    if box_e is not None:
        raise box_e
    return None, False


# Model nào timeout mạng 2 lần liên tiếp → nghỉ 5 phút (đừng hammer model chậm,
# xoay sang model nhanh hơn ngay — hết treo 70s×5 lần như log lỗi).
_SLOW = {}
_SLOW_LOCK = threading.Lock()
_NOTOOLS = {}  # model báo 400 tool-unsupported → cấm gọi kèm tools trong 1h
_NOTOOLS_TTL = 3600


def _notools_active(model):
    try:
        with _SLOW_LOCK:
            return _NOTOOLS.get(model, 0) > time.time()
    except Exception:
        return False


def _note_notools(model):
    try:
        with _SLOW_LOCK:
            _NOTOOLS[model] = time.time() + _NOTOOLS_TTL
    except Exception:
        pass


def _slow_cooldown(model):
    try:
        with _SLOW_LOCK:
            return _SLOW.get(model, 0) > time.time()
    except Exception:
        return False


def _note_slow(model):
    try:
        with _SLOW_LOCK:
            n = _SLOW.get(model + "#n", 0) + 1
            _SLOW[model + "#n"] = n
            if n >= 2:
                _SLOW[model] = time.time() + 300
                _SLOW[model + "#n"] = 0
    except Exception:
        pass


def _note_fast(model):
    try:
        with _SLOW_LOCK:
            _SLOW.pop(model, None)
            _SLOW.pop(model + "#n", None)
    except Exception:
        pass


# ── Tham số theo tài liệu Groq cho reasoning models ──────────────────────
# https://console.groq.com/docs/reasoning : temperature 0.5-0.7 + top_p 0.95
# để reasoning ổn định (mặc định dễ lặp/vỡ); reasoning_effort low/medium/high
# cho gpt-oss và qwen3.8 (qwen3.6 chỉ none/default nên không gắn effort).
_REASON_TEMP = 0.6
_REASON_TOP_P = 0.95


def _is_reason_model(model):
    m = model or ""
    return m.startswith("openai/gpt-oss") or m.startswith("qwen/qwen3")


def _apply_reason_params(model, body):
    """Gắn tham số docs Groq cho reasoning models. Chỉ gắn khi body chưa tự set."""
    body = dict(body)
    if not _is_reason_model(model):
        return body
    body.setdefault("temperature", _REASON_TEMP)
    body.setdefault("top_p", _REASON_TOP_P)
    effort = os.environ.get("REM_REASONING", "low").strip().lower()
    if effort in ("low", "medium", "high") and (
            model.startswith("openai/gpt-oss") or model.startswith("qwen/qwen3.8")):
        body["reasoning_effort"] = effort
    return body


def _post(body, model, timeout=30, budget=None, cancel=None, stream=False):
    """Gọi Groq với KeyManager: weighted key selection, circuit breaker, smart retry.
    cancel: threading.Event của ESC//stop → hủy trong ≤0.5s (không treo hết timeout).
    stream: True → requests stream + timeout (connect, read) ngắn để ReadTimeout
      không treo 70s; model timeout 2 lần → nghỉ 5 phút, xoay model khác ngay."""
    if _is_cancelled(cancel):
        return None
    if _slow_cooldown(model):
        return None
    if _notools_active(model) and isinstance(body, dict) and body.get("tools"):
        return "NOTOOLS"  # model này đã báo không hỗ trợ tools — khỏi đốt request
    body = _apply_reason_params(model, body)
    ks = keys()
    if not ks:
        return None
    last = None
    attempts = 0
    saw_rate = False  # lượt này đã gặp 429 key nào chưa (để đếm rotation cứu lượt)
    rate_hit = set()  # key nào 429/hết giới hạn lượt này → loại khỏi vòng pick, XOAY NGAY key khác
    # 23 key độc lập → thử TRỌN pool trước khi bỏ cuộc (mỗi key chỉ chịu ~1 request
    # /phút vì 1 request ăn ~10K token ≈ trọn 8K TPM). Không giới hạn 16 như cũ.
    max_attempts = len(ks) + 8
    end = time.time() + (budget if budget and budget > 0 else 75)
    import random as _rd
    # Stream: chờ connect 10s + giữa các chunk 30s là đủ (Groq thường trả chunk <5s).
    # Non-stream (text/vision/chat): (10, 25). Không còn timeout 70s treo lượt.
    if stream:
        tmo = (10, 30)
    elif isinstance(timeout, (list, tuple)):
        tmo = tuple(timeout)
    else:
        try:
            tmo = (10, max(15, min(int(timeout), 25)))
        except Exception:
            tmo = (10, 25)
    while time.time() < end and attempts < max_attempts:
        if _is_cancelled(cancel):
            return None
        attempts += 1
        choice, wait = _km.pick_key(ks, exclude=rate_hit)
        if choice is None:
            break
        if choice in rate_hit:
            # pick_key chỉ trả key vừa 429 khi CẢ POOL đã cạn → ngừng quay,
            # báo RATE cho caller (chat/chat_stream) backoff rồi thử lại đợt sau.
            last = "RATE"
            break
        if wait > 0:
            if _sleep_cancel(min(wait, max(0.5, end - time.time())), cancel, end):
                return None
            continue
        try:
            r, was_cancel = _post_cancelable(
                "https://api.groq.com/openai/v1/chat/completions",
                {"Authorization": f"Bearer {choice}"},
                {"model": model, **body}, tmo, cancel, stream=stream, end=end,
            )
            if was_cancel:
                return None
            if r is None:
                last = "TIMEOUT"
                _note_slow(model)
                continue
            _km.note_headers(choice, getattr(r, "headers", None))  # quota thật → né trước 429
            if r.status_code == 200:
                _km.report_success(choice)
                if saw_rate:
                    _km.note_rotation()  # 429 key này → thành công bằng key khác = XOAY CỨU LƯỢT
                _note_fast(model)
                return r
            if r.status_code in _RATE:
                retry = r.headers.get("retry-after")
                _km.report_rate_limit(choice, retry)
                # Key ĐỘC LẬP (thực nghiệm: key A 429 nhưng key B vẫn 200) →
                # chỉ đánh dấu key đó nghỉ ngắn, KHÔNG khóa cả pool, xoay key khác ngay.
                # rate_hit: key này hết giới hạn lượt này → các vòng sau loại hẳn,
                # không thử lại key vừa 429 trong cùng 1 request.
                rate_hit.add(choice)
                saw_rate = True
                last = "RATE"
                continue
            elif r.status_code in _SERVER_ERRS:
                # server quá tải → chờ ngắn + jitter rồi thử key khác (không chặn key)
                wait = min(2 ** min(attempts, 4), 8) * _rd.uniform(0.7, 1.3)
                if _sleep_cancel(min(wait, max(0.2, end - time.time())), cancel, end):
                    return None
                _km.report_failure(choice, is_server=True)
                last = f"HTTP {r.status_code}"
                continue
            else:
                last = f"HTTP {r.status_code}"
                # 4xx deterministic: context tràn (prompt_ctx_length), tool_use_failed,
                # bad request... thử 23 key VÔ ÍCH (lỗi do request, không do key).
                # Lấy message lỗi rõ ràng cho agent tự xử (compact context...).
                _err_msg = ""
                try:
                    _ej = r.json()
                    _err_msg = (_ej.get("error") or {}).get("message") or _ej.get("message") or ""
                except Exception:
                    _err_msg = ""
                _ldet = f"HTTP {r.status_code}: {_err_msg[:300]}" if _err_msg else f"HTTP {r.status_code}"
                _elow = (_err_msg or "").lower()
                if r.status_code == 400 and "tool" in _elow and "support" in _elow:
                    # Model này KHÔNG hỗ trợ tool calling — KHÔNG phải lỗi request
                    # cố định: cấm model này (kèm tools) 1h, báo caller xoay model khác
                    # ngay thay vì bỏ cuộc với [LOI HTTP 400...].
                    _note_notools(model)
                    _km.report_failure(choice)
                    return "NOTOOLS"
                if r.status_code == 400 and _err_msg and ("context" in _err_msg.lower()
                                                          or "token" in _err_msg.lower()
                                                          or "tool" in _err_msg.lower()):
                    _set_det(True, _ldet)
                    break
                if r.status_code in (401, 403):
                    _km.report_auth_fail(choice)
                    break
                if r.status_code >= 400 and r.status_code < 500 and r.status_code != 408:
                    # 400/404/422... lỗi request cố định → không retry key khác
                    _km.report_failure(choice)
                    break
                _km.report_failure(choice)
        except Exception as e:
            if _is_cancelled(cancel):
                return None
            last = type(e).__name__
            _ldet = f"{type(e).__name__}: {e}"
            _km.report_failure(choice, is_network=True)
            # payload quá lớn → write-timeout/conn-abort: KHÔNG phải model chậm,
            # không gắn cờ slow, không thử hết key — chặn ngay (deterministic).
            if _ctx_guard(body.get("messages") or []):
                _set_det(True, _ldet)
                break
            if last in ("ReadTimeout", "ConnectTimeout"):
                # Model chậm/kẹt → ghi slow (2 lần là nghỉ 5p), KHÔNG sleep hammer
                # lại model đó; vòng sau chat_stream xoay sang model khác ngay.
                _note_slow(model)
                continue
            if last in _NET_ERRS:
                if _sleep_cancel(0.5 * _rd.uniform(0.7, 1.3), cancel, end):
                    return None
    if last == "RATE":
        return "RATE"
    if last and (last not in _SERVER_ERRS):
        # Timeout lẻ tẻ giữa nhiều model là bình thường (đã xoay model khác) —
        # chỉ báo khi không còn là timeout để khỏi spam log như ảnh lỗi.
        if last not in ("TIMEOUT", "ReadTimeout", "ConnectTimeout"):
            print(f"[groq] that bai ({model}): {last}", file=__import__("sys").stderr)
    return None


def _ctx_guard(msgs):
    """Pre-check độ dài prompt so với cửa sổ context gpt-oss (131K token, ~400K bytes UTF-8).
    Prompt quá lớn → sớm cắt/thông báo thay vì để Groq trả 400 context_length_exceeded
    hoặc connection-write-timeout rồi đốt 23 key. Trả chuỗi cảnh báo hoặc ''."""
    try:
        total = sum(len((m.get("content") or "").encode("utf-8", "replace"))
                    for m in msgs if isinstance(msgs, list))
        # ~3 bytes/token tiếng Việt → 131K token ≈ 350-400K bytes; giữ ngưỡng an toàn
        if total > 350_000:
            return (f"[CANH BAO] prompt quá dài ({total/1000:.0f} KB bytes ≈ vượt ~131K token). "
                    "Hãy TÓM TẮT/triệt nội dung cũ trước khi gọi lại, KHÔNG gửi trọn.")
    except Exception:
        pass
    return ""


def chat(msgs, tools=None, budget=None, cancel=None):
    """Trả về message của model đầu tiên trả lời được trong khung thời gian budget."""
    msgs = _strip_reasoning(msgs)
    _warn = _ctx_guard(msgs)
    if _warn:
        # Context quá dài → không gửi gì cả (tiết kiệm 23 key + budget), báo rõ cho agent tự compact
        return {"role": "assistant", "content": _warn}
    body = {"messages": msgs, "max_tokens": 16384}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    chain = chat_models() + [m for m in fb_models() if m not in chat_models()]
    end = time.time() + (budget if budget and budget > 0 else 75)
    for attempt in range(3):
        if time.time() >= end or _is_cancelled(cancel):
            return None
        all_rate = True
        for m in chain[:5]:
            if time.time() >= end or _is_cancelled(cancel):
                break
            if _slow_cooldown(m):
                continue
            r = _post(body, m, budget=max(1, end - time.time()), cancel=cancel)
            if _is_cancelled(cancel):
                return None
            if r == "RATE":
                continue
            if r == "NOTOOLS":
                continue  # model không hỗ trợ tools → model kế tiếp
            if _det_flag():
                # Lỗi request cố định (context tràn...) — agent cần SỬA request, không thử tiếp
                return ({"role": "assistant", "content": f"[LOI {_det_msg()}]\n{_warn}"}
                        if _det_msg() else None)
            if r is not None:
                _data = r.json()
                _note_usage(_data.get("usage"))
                _msg = _data["choices"][0]["message"]
                # gpt-oss trả thêm trường "reasoning" nhưng agent chỉ dùng
                # content/tool_calls (/think đọc từ stream) → bỏ để gọn context
                if isinstance(_msg, dict):
                    _msg.pop("reasoning", None)
                return _msg
            all_rate = False
        if all_rate:
            wait = min(1 + attempt, 4)
            if _sleep_cancel(max(0.0, min(wait, end - time.time())), cancel, end):
                return None
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


def _iter_stream(r, on_delta, cancel=None):
    """Tiêu thụ response stream Groq → message cuối. on_delta(ev) nhận
    {"type":"content"|"thinking","text":...} để UI hiện tiến độ chữ trực tiếp.
    cancel (ESC//stop) → dừng đọc ngay, trả phần đã nhận (không mất chữ).
    Stall >30s không chunk (ReadTimeout từ requests) → cũng trả phần đã nhận
    thay vì return None mất trắng như bản cũ."""
    acc = {"content": "", "thinking": "", "tc": []}

    def emit(kind, s):
        if on_delta and s:
            try:
                on_delta({"type": kind, "text": s})
            except Exception:
                pass

    try:
        for raw in r.iter_lines():
            if _is_cancelled(cancel):
                break
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
            if d.get("usage"):
                _note_usage(d["usage"])
            ch = (d.get("choices") or [{}])[0]
            delta = ch.get("delta") or {}
            # Groq gpt-oss trả reasoning ở `delta.reasoning` (field chính thức);
            # `reasoning_content` chỉ là tên LangChain ánh xạ — đọc cả 2 cho chắc.
            r_text = delta.get("reasoning") or delta.get("reasoning_content")
            if r_text:
                acc["thinking"] += r_text
                emit("thinking", r_text)
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
        pass  # stall/timeout giữa stream → giữ phần đã nhận, không mất trắng
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


def chat_stream(msgs, tools=None, budget=None, on_delta=None, cancel=None):
    """Gọi Groq dạng STREAM — UI thấy chữ/tiến độ đang chảy, không bị 'treo im'.
    Trả message cuối giống chat(), kèm on_delta để cập nhật tiến độ.
    max_tokens 8192: phản hồi lớn (tool_calls + suy luận) không bị cắt giữa chừng.
    Có thể ép model qua env REM_MODEL=openai/gpt-oss-20b.
    cancel: ESC//stop hủy trong ≤0.5s. Model chậm (timeout 2 lần) bị bỏ qua 5 phút,
    xoay sang model nhanh ngay thay vì treo 70s×5 như bản cũ."""
    body = {"messages": msgs, "max_tokens": 8192, "stream": True,
            "stream_options": {"include_usage": True}}
    try:
        _warn = _ctx_guard(msgs if isinstance(msgs, list) else [])
    except Exception:
        _warn = ""
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    chain = chat_models() + [m for m in fb_models() if m not in chat_models()]
    ov = os.environ.get("REM_MODEL", "").strip()
    if ov:
        chain = [ov] + [m for m in chain if m != ov]
    end = time.time() + (budget if budget and budget > 0 else 75)
    for attempt in range(6):
        if time.time() >= end or _is_cancelled(cancel):
            return None
        all_rate = True
        skipped_slow = 0
        for m in chain[:5]:
            if time.time() >= end or _is_cancelled(cancel):
                break
            if _slow_cooldown(m) or (_notools_active(m) and tools):
                skipped_slow += 1
                continue
            r = _post(body, m, budget=max(1, end - time.time()), timeout=(10, 30),
                      cancel=cancel, stream=True)
            if _is_cancelled(cancel):
                return None
            if _det_flag():
                # Lỗi request cố định (context tràn...) — dừng ngay, trả lỗi để agent
                # tự compact/trim thay vì tự thử lại hết 23 key.
                if on_delta:
                    try:
                        on_delta({"type": "content", "text": f"[LOI {_det_msg()}]\n{_warn}"})
                    except Exception:
                        pass
                return {"role": "assistant",
                        "content": f"[LOI {_det_msg()}]\n{_warn}"}
            if r == "RATE":
                # Key độc lập → 429 1 key chỉ cần xoay: thử model kế tiếp ngay
                # (mỗi model lại xoay qua 23 key ở _post). KHÔNG ngủ chờ org.
                continue
            if r == "NOTOOLS":
                # Model không hỗ trợ tool calling → model kế tiếp ngay, không báo lỗi
                all_rate = False
                continue
            if r is None:
                all_rate = False
                continue
            all_rate = False
            if cancel is not None:
                # Đóng response khi ESC//stop để iter_lines đang chặn bung ngay
                # (nếu không, hủy phải chờ chunk kế tiếp mới check được).
                try:
                    import weakref as _wr
                    _rref = _wr.ref(r)
                    _cev = cancel
                    def _closer(_rr=_rref, _cc=_cev):
                        try:
                            while not _cc.is_set():
                                time.sleep(0.1)
                            _ro = _rr()
                            if _ro is not None:
                                try:
                                    _ro.close()
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    threading.Thread(target=_closer, daemon=True).start()
                except Exception:
                    pass
            out = _iter_stream(r, on_delta, cancel=cancel)
            if _is_cancelled(cancel):
                return None
            if out is not None:
                return out
            # stream stall nhưng chưa có gì → thử model kế tiếp ngay
            if _is_cancelled(cancel):
                return None
            continue
        if _is_cancelled(cancel):
            return None
        # tất cả model chậm/timeout (không phải RATE) → chờ ngắn rồi thử lại,
        # ưu tiên model nhanh ở vòng sau (slow-cooldown đã loại model kẹt)
        if all_rate:
            wait = min(1 + attempt, 4)  # giam backoff: 1s, 2s, 3s
            if _sleep_cancel(max(0.0, min(wait, end - time.time())), cancel, end):
                return None
            continue
        # hết vòng mà toàn timeout/model-chậm → nghỉ 2s cho model hồi rồi thử tiếp
        if skipped_slow >= 3 and time.time() < end:
            if _sleep_cancel(min(2.0, end - time.time()), cancel, end):
                return None
    return None


def text(p, max_tokens=700, temp=0.2, budget=None, cancel=None):
    end = time.time() + (budget if budget and budget > 0 else 75)
    for m in clone_models()[:3]:
        if time.time() >= end or _is_cancelled(cancel):
            break
        if _slow_cooldown(m):
            continue
        r = _post({"messages": [{"role": "user", "content": p}], "max_tokens": max_tokens, "temperature": temp}, m, 20, budget=max(1, end - time.time()), cancel=cancel)
        if _is_cancelled(cancel):
            return ""
        if r is not None and r != "RATE" and r.status_code == 200:
            _data = r.json()
            _note_usage(_data.get("usage"))
            content = _data["choices"][0]["message"].get("content")
            if content:
                return content
            # content rỗng → thử model kế tiếp
            continue
    return ""


def vision(q, img, budget=None, cancel=None):
    try:
        d = __import__("base64").b64encode(open(img, "rb").read()).decode()
    except Exception:
        return None
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": q},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{d}"}}]}], "max_tokens": 300}
    end = time.time() + (budget if budget and budget > 0 else 75)
    for m in vision_models()[:3]:
        if time.time() >= end or _is_cancelled(cancel):
            break
        if _slow_cooldown(m):
            continue
        r = _post(body, m, 25, budget=max(1, end - time.time()), cancel=cancel)
        if _is_cancelled(cancel):
            return None
        if r == "RATE":
            return None
        if r is not None and r.status_code == 200:
            _data = r.json()
            _note_usage(_data.get("usage"))
            return _data["choices"][0]["message"]["content"]
    return None


_seeded = seed_defaults()
if _seeded:
    print(f"[groq] da nap {_seeded} Groq key mac dinh (da ma hoa trong DB).", file=__import__("sys").stderr)
