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


_RR = [0]
_COOL = {}  # key -> cooldown until ts
_RATE = (413, 429)
# Lỗi mạng/read-timeout: KHÔNG retry dồn dập — thoát nhanh cho khỏi treo
_NET_ERRS = ("ReadTimeout", "ConnectTimeout", "ConnectionError", "ReadError",
             "RemoteDisconnected", "ChunkedEncodingError", "ProxyError")


def _post(body, model, timeout=30, budget=None):
    """Gọi Groq, vòng key round-robin với cooldown rate-limit.
    budget (giây) giới hạn cứng tổng thời gian — tránh treo lâu không tự thoát."""
    ks = keys()
    if not ks:
        return None
    n = len(ks)
    last = None
    net_fail = 0
    end = time.time() + (budget if budget and budget > 0 else 75)
    while time.time() < end:
        now = time.time()
        choice = None
        for i in range(n):
            k = ks[(_RR[0] + i) % n]
            if _COOL.get(k, 0) <= now:
                choice = k
                _RR[0] = (_RR[0] + 1) % n
                break
        if choice is None:
            # tất cả keys đang cooldown → chờ key sớm hết hạn nhất (không quá budget)
            wake = min(_COOL.values())
            d = max(0.5, min(15.0, wake - now))
            time.sleep(min(d, max(0.5, end - time.time())))
            continue
        to = max(1, min(timeout, int(end - time.time() + 1)))
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {choice}"},
                json={"model": model, **body}, timeout=to,
            )
            if r.status_code == 200:
                net_fail = 0
                return r
            if r.status_code in _RATE:
                retry = r.headers.get("retry-after")
                _COOL[choice] = time.time() + (max(30, int(retry)) if retry else 50)
                last = "RATE"
            else:
                last = f"HTTP {r.status_code}"
                # lỗi key (401) hoặc model — không phải rate, dừng nhanh
                if r.status_code in (401, 403):
                    break
        except Exception as e:
            last = type(e).__name__
            net_fail += 1
            # mạng/timeout liên tiếp → đừng retry dồn dập, thoát nhanh
            if last in _NET_ERRS and net_fail >= 2:
                break
    if last == "RATE":
        return "RATE"
    if last:
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
        for m in chain[:4]:
            if time.time() >= end:
                break
            r = _post(body, m, budget=max(1, end - time.time()))
            if r == "RATE":
                last = "RATE"
                continue
            if r is not None:
                return r.json()["choices"][0]["message"]
            all_rate = False
# hết model nào còn dùng được tạm → backoff ngắn rồi thử lại tới hết budget
        if all_rate:
            wait = min(2 + attempt * 2, 8)
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
        for m in chain[:4]:
            if time.time() >= end:
                break
            r = _post(body, m, budget=max(1, end - time.time()), timeout=70)
            if r == "RATE":
                continue
            if r is None:
                all_rate = False
                continue
            all_rate = False
            return _iter_stream(r, on_delta)
        if all_rate:
            wait = min(2 + attempt * 2, 8)
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