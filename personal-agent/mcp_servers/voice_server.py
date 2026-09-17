import os, sys, re, json, shutil, subprocess, importlib.util

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcplib import Server, Tool, schema, clamp  # noqa: F401 (clamp dùng khi cắt log dài)
from config import DIR

# Voice: mic/STT + TTS tiếng Việt — điều khiển Remtm bằng giọng nói,
# song song với gõ phím. Luồng: mic → text → Agent.run → TTS phát loa.
# STT backend (thử dần, thiếu thì báo pip, KHÔNG crash ở import):
#   1. SpeechRecognition + Google (online, không key, vi-VN)
#   2. faster-whisper small (offline)
#   3. vosk (offline, cần model)

OUT = os.path.join(DIR, "voice")
for d in (OUT, os.path.join(OUT, "wav"), os.path.join(OUT, "audio")):
    os.makedirs(d, exist_ok=True)

VOICE_VI = "vi-VN-NamMinhNeural"
LANG_DEFAULT = "vi-VN"

REC_SECONDS_MIN, REC_SECONDS_MAX = 1, 30

DANGEROUS_HINTS = ("rm -rf", "xóa hết", "xoa het", "format", "shutdown",
                   "poweroff", "reboot", "mkfs", "dd if=")


def _have(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _which(*names):
    for n in names:
        if shutil.which(n):
            return n
    return ""


def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"lệnh không tồn tại: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout sau {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def _mic_info():
    """Kiểm tra mic qua arecord/pactl/PyAudio — chỉ đọc, không thu âm."""
    parts = []
    rc, out = _run(["arecord", "-l"], timeout=10)
    if rc == 0 and "card" in out.lower():
        cards = [l.strip() for l in out.splitlines() if "card" in l.lower()][:4]
        parts.append("arecord: OK (" + "; ".join(cards) + ")")
    elif rc == 0:
        parts.append("arecord: có lệnh nhưng không thấy card thu âm")
    else:
        parts.append("arecord: THIẾU/không chạy (apt install alsa-utils)")
    rc2, out2 = _run(["pactl", "list", "short", "sources"], timeout=10)
    if rc2 == 0 and out2.strip():
        n = len([l for l in out2.splitlines() if l.strip()])
        parts.append(f"pactl: OK ({n} source PipeWire/Pulse)")
    else:
        parts.append("pactl: không thấy source (pipewire-pulse chưa chạy?)")
    if _have("pyaudio"):
        try:
            import pyaudio
            pa = pyaudio.PyAudio()
            cnt = pa.get_host_api_count()
            pa.terminate()
            parts.append(f"PyAudio: OK ({cnt} host API)")
        except Exception as e:
            parts.append(f"PyAudio: cài rồi nhưng lỗi ({e})")
    else:
        parts.append("PyAudio: THIẾU (pip install pyaudio; cần libportaudio2 + portaudio19-dev)")
    return parts


def _stt_info():
    parts = []
    if _have("speech_recognition"):
        parts.append("SpeechRecognition+Google vi-VN: OK (online, không key)")
    else:
        parts.append("SpeechRecognition: THIẾU (pip install SpeechRecognition)")
    if _have("faster_whisper"):
        parts.append("faster-whisper small: OK (offline)")
    else:
        parts.append("faster-whisper: THIẾU — optional (pip install faster-whisper)")
    if _have("vosk"):
        m = _vosk_model()
        parts.append("vosk: OK (model: %s)" % m if m else
                     "vosk: có lib, THIẾU model (tải model vi về ~/.rem_ai/vosk/)")
    else:
        parts.append("vosk: THIẾU — optional (pip install vosk)")
    return parts


def _tts_info():
    parts = []
    parts.append("edge-tts: " + ("OK" if _have("edge_tts") else "THIẾU (pip install edge-tts)"))
    parts.append("gTTS fallback: " + ("OK" if _have("gtts") else "THIẾU (pip install gTTS)"))
    spk = _which("ffplay", "aplay")
    parts.append("loa phát: " + (f"OK ({spk})" if spk else "THIẾU (cần ffplay từ ffmpeg hoặc aplay từ alsa-utils)"))
    return parts


def voice_status():
    """Kiểm tra mic, STT backend, TTS, loa — không thu, không phát, không crash."""
    lines = ["== MIC =="] + ["  " + p for p in _mic_info()]
    lines += ["== STT =="] + ["  " + p for p in _stt_info()]
    lines += ["== TTS/LOA =="] + ["  " + p for p in _tts_info()]
    lines.append(f"== THƯ MỤC ==\n  {OUT}")
    return clamp("\n".join(lines), 3000)


def _clamp_seconds(seconds):
    try:
        s = int(float(seconds))
    except Exception:
        s = 6
    return max(REC_SECONDS_MIN, min(REC_SECONDS_MAX, s))


def _record_wav(seconds, dest):
    """Thu mic bằng arecord ra wav 16k mono. Trả (ok, msg)."""
    if not shutil.which("arecord"):
        return False, "[LOI] Chưa có lệnh arecord — cài: sudo apt install alsa-utils"
    seconds = _clamp_seconds(seconds)
    cmd = ["arecord", "-D", "default", "-f", "S16_LE", "-r", "16000",
           "-c", "1", "-d", str(seconds), "-q", dest]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=seconds + 15)
    except subprocess.TimeoutExpired:
        return False, f"[LOI] Thu âm quá hạn ({seconds}s) — mic có bị giữ bởi app khác không?"
    except Exception as e:
        return False, f"[LOI] Không thu được mic ({type(e).__name__}: {e})"
    if p.returncode != 0:
        err = ((p.stderr or "") + (p.stdout or "")).strip()[-300:]
        return False, (f"[LOI] arecord lỗi (mã {p.returncode}): {err}\n"
                       "Gợi ý: kiểm tra mic bằng 'arecord -l' và 'pactl list short sources'.")
    try:
        if not os.path.exists(dest) or os.path.getsize(dest) < 1000:
            return False, "[LOI] File thu âm rỗng — mic có đang bị mute không? (kiểm tra pavucontrol/alsamixer)"
    except Exception as e:
        return False, f"[LOI] {type(e).__name__}: {e}"
    # Cảnh báo file quá lặng (toàn số 0) — thường do sai source/mute
    try:
        with open(dest, "rb") as f:
            raw = f.read()
        if raw and max(raw) == 0:
            return False, "[LOI] Mic thu được toàn im lặng — kiểm tra mic mặc định trong pavucontrol."
    except Exception:
        pass
    return True, f"OK {dest}"


def _stt_google(wav, lang):
    import speech_recognition as sr
    r = sr.Recognizer()
    with sr.AudioFile(wav) as src:
        audio = r.record(src)
    try:
        out = r.recognize_google(audio, language=lang, show_all=True)
    except Exception:
        # show_all=True có thể lỗi ở bản cũ → thử lại dạng text thuần
        text = r.recognize_google(audio, language=lang)
        return text, 0.0, "google"
    if isinstance(out, dict):
        alts = out.get("alternative") or []
        if alts:
            top = alts[0]
            return top.get("transcript", ""), float(top.get("confidence", 0.0) or 0.0), "google"
        return "", 0.0, "google"
    return str(out or ""), 0.0, "google"


def _stt_faster_whisper(wav, lang):
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    code = {"vi-vn": "vi", "vi": "vi"}.get(str(lang).lower(), "vi")
    segs, _info = model.transcribe(wav, language=code, beam_size=5)
    text = " ".join(s.text.strip() for s in segs).strip()
    return text, 0.0, "faster-whisper-small"


def _vosk_model():
    cands = [os.environ.get("VOSK_MODEL", ""),
             os.path.join(DIR, "vosk"),
             os.path.join(os.path.expanduser("~"), ".rem_ai", "vosk"),
             os.path.join(os.path.expanduser("~"), ".cache", "vosk")]
    for c in cands:
        if c and os.path.isdir(c):
            # chấp nhận thư mục chứa model trực tiếp hoặc chứa các model con
            try:
                subs = os.listdir(c)
            except Exception:
                continue
            if any(x in ("am", "conf", "graph") for x in subs):
                return c
            for s in subs:
                if os.path.isdir(os.path.join(c, s)):
                    return os.path.join(c, s)
    return ""


def _stt_vosk(wav, lang):
    import wave, json as _json
    from vosk import Model, KaldiRecognizer
    model_path = _vosk_model()
    if not model_path:
        raise RuntimeError("chưa có model vosk tiếng Việt trong ~/.rem_ai/vosk/")
    wf = wave.open(wav, "rb")
    try:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != 16000:
            raise RuntimeError("wav phải 16k mono 16-bit (arecord đã tạo đúng, file có bị ghi đè không?)")
        rec = KaldiRecognizer(Model(model_path), wf.getframerate())
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            rec.AcceptWaveform(data)
        res = _json.loads(rec.FinalResult() or "{}")
        return (res.get("text") or "").strip(), 0.0, "vosk"
    finally:
        try:
            wf.close()
        except Exception:
            pass


def voice_listen(seconds=6, lang="vi-VN"):
    """Thu mic `seconds` giây rồi nhận dạng giọng nói (Google → faster-whisper → vosk).

    Trả text đã nghe (kèm confidence + backend nếu có)."""
    seconds = _clamp_seconds(seconds)
    lang = (lang or LANG_DEFAULT).strip() or LANG_DEFAULT
    tag = __import__("time").strftime("%y%m%d-%H%M%S")
    wav = os.path.join(OUT, "wav", f"listen_{tag}.wav")
    ok, msg = _record_wav(seconds, wav)
    if not ok:
        return msg
    errors = []
    # 1) Google online qua SpeechRecognition
    if _have("speech_recognition"):
        try:
            text, conf, be = _stt_google(wav, lang)
            if text.strip():
                suf = f" (độ tin cậy {conf:.0%})" if conf > 0 else ""
                return f"[{be}] Nghe được{suf}: {text.strip()}\nFile: {wav}"
            errors.append("google: không nghe rõ (im lặng/ồn)")
        except Exception as e:
            errors.append(f"google lỗi ({type(e).__name__}: {str(e)[:150]} — cần mạng để dùng backend này)")
    else:
        errors.append("google: chưa cài (pip install SpeechRecognition)")
    # 2) faster-whisper offline
    if _have("faster_whisper"):
        try:
            text, _c, be = _stt_faster_whisper(wav, lang)
            if text.strip():
                return f"[{be}] Nghe được: {text.strip()}\nFile: {wav}"
            errors.append("faster-whisper: không nghe rõ")
        except Exception as e:
            errors.append(f"faster-whisper lỗi ({type(e).__name__}: {str(e)[:150]})")
    # 3) vosk offline
    if _have("vosk"):
        try:
            text, _c, be = _stt_vosk(wav, lang)
            if text.strip():
                return f"[{be}] Nghe được: {text.strip()}\nFile: {wav}"
            errors.append("vosk: không nghe rõ")
        except Exception as e:
            errors.append(f"vosk lỗi ({type(e).__name__}: {str(e)[:150]})")
    hint = ("Chưa cài backend STT nào: pip install SpeechRecognition "
            "(online) hoặc faster-whisper/vosk (offline).") \
        if not (_have("speech_recognition") or _have("faster_whisper") or _have("vosk")) else ""
    detail = "\n".join("  - " + e for e in errors)
    return (f"[LOI] Không nhận dạng được giọng nói (file giữ lại: {wav}).\n{detail}\n"
            f"{hint}\nGợi ý: nói to/rõ hơn, để mic gần, thử lại với seconds lớn hơn.".rstrip())


def _media_tts(text, path, voice=""):
    """Tái dùng media_tools.tts (edge-tts vi-VN + gTTS fallback)."""
    try:
        from mcp_servers.media_tools import tts as _tts, dur as _dur
    except Exception:
        try:
            from media_tools import tts as _tts, dur as _dur  # chạy lẻ trong thư mục mcp_servers
        except Exception as e:
            return f"[LOI] Không tải được media_tools.tts: {e}"
    try:
        r = _tts(text, path, voice=voice or VOICE_VI)
    except Exception as e:
        return f"[LOI] TTS thất bại ({type(e).__name__}: {e})"
    if isinstance(r, str) and r.startswith("[LOI]"):
        return r
    try:
        d = _dur(path)
    except Exception:
        d = 0.0
    return f"OK {path} ({d:.1f}s)"


def _play_background(path):
    """Phát mp3 nền (nohup, không treo server). Trả mô tả cách phát."""
    if _which("ffplay"):
        cmd = ["nohup", "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]
        player = "ffplay"
    elif _which("aplay"):
        # aplay không đọc mp3 → đổi sang wav rồi phát
        wav = os.path.splitext(path)[0] + "_play.wav"
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path,
                            "-ar", "44100", "-ac", "2", wav],
                           check=True, timeout=60)
            path = wav
        except Exception as e:
            return f"[LOI] Không có ffplay; đổi mp3→wav cho aplay thất bại ({e}) — cài ffmpeg."
        cmd = ["nohup", "aplay", "-q", path]
        player = "aplay"
    else:
        return "[LOI] Không có trình phát loa (cài ffmpeg để có ffplay, hoặc alsa-utils để có aplay)."
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        return f"OK ({player} nền)"
    except Exception as e:
        return f"[LOI] Không phát được loa ({type(e).__name__}: {e})"


def voice_say(text, voice=""):
    """Đọc `text` tiếng Việt ra loa (tái dùng media_tools.tts + phát nền).

    Trả đường dẫn mp3 + thời lượng + trạng thái phát."""
    text = (text or "").strip()
    if not text:
        return "[LOI] text rỗng — cần nội dung để đọc"
    if len(text) > 2000:
        return "[LOI] text quá dài (tối đa 2000 ký tự/lần) — chia nhỏ ra"
    tag = __import__("time").strftime("%y%m%d-%H%M%S")
    name = re.sub(r"[^\w]+", "_", text[:30]).strip("_") or "say"
    path = os.path.join(OUT, "audio", f"{tag}_{name}.mp3")
    r = _media_tts(text, path, voice=voice or "")
    if isinstance(r, str) and r.startswith("[LOI]"):
        return r
    m = re.search(r"\(([\d.]+)s\)", r or "")
    dur_s = m.group(1) + "s" if m else "?"
    play = _play_background(path)
    if play.startswith("[LOI]"):
        return f"Đã tạo {path} ({dur_s}) nhưng {play}"
    return f"OK {path} ({dur_s}) — đang phát loa {play[4:]}"


_WAKE = re.compile(r"^(rem\s*ơi|rem\s*oi|hey\s*rem|rem)\s*[,:\-–—]?\s*", re.I)


def voice_cmd(seconds=6):
    """Tiện ích 1 phát 'nghe → trả text lệnh': thu mic, STT, chuẩn hóa.

    Agent/Repl dùng text này làm câu lệnh cho Agent.run (full tool).
    Lệnh nguy hiểm được gắn cờ [XÁC NHẬN] chứ KHÔNG tự chạy."""
    raw = voice_listen(seconds=seconds)
    if raw.startswith("[LOI]"):
        return raw
    m = re.search(r"Nghe được[^:]*:\s*(.+?)\n", raw, re.S)
    heard = (m.group(1).strip() if m else raw.strip())
    norm = _WAKE.sub("", heard).strip()
    norm = re.sub(r"\s+", " ", norm)
    low = norm.lower()
    flag = ""
    if any(h in low for h in DANGEROUS_HINTS):
        flag = ("\n[XÁC NHẬN] Lệnh có vẻ nguy hiểm — hỏi lại người dùng "
                "trước khi chạy (giữ nguyên tắc xác nhận lệnh nguy hiểm).")
    be = re.search(r"^\[([^\]]+)\]", raw)
    be = be.group(1) if be else "?"
    return f"Lệnh thoại ({be}): {norm}{flag}"


TOOLS = [
    Tool("voice_status", "Kiểm tra mic (arecord/pactl/PyAudio), backend STT, TTS và loa. Không thu, không phát.",
         schema({}), voice_status),
    Tool("voice_listen", "Thu mic rồi nhận dạng giọng nói (Google vi-VN → faster-whisper → vosk). Trả text đã nghe.",
         schema({"seconds": {"type": "integer", "description": "số giây thu, 1-30, mặc định 6"},
                 "lang": {"type": "string", "description": "mã ngôn ngữ, mặc định vi-VN"}},
                required=["seconds"]),
         voice_listen),
    Tool("voice_say", "Đọc text tiếng Việt ra loa (tái dùng media_tools.tts, phát nền không treo).",
         schema({"text": {"type": "string", "description": "nội dung cần đọc (tối đa 2000 ký tự)"},
                 "voice": {"type": "string", "description": "giọng edge-tts, mặc định vi-VN-NamMinhNeural"}},
                required=["text"]),
         voice_say),
    Tool("voice_cmd", "Nghe 1 lệnh thoại rồi trả text lệnh đã chuẩn hóa (bỏ wake-word 'Rem ơi', gắn cờ lệnh nguy hiểm).",
         schema({"seconds": {"type": "integer", "description": "số giây thu, 1-30, mặc định 6"}},
                required=["seconds"]),
         voice_cmd),
]

if __name__ == "__main__":
    Server(TOOLS, "voice", "0.1.0").serve(sys.stdin, sys.stdout)
