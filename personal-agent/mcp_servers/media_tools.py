import os, sys, re, json, asyncio, random, subprocess, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcplib import Server, Tool, schema, clamp
from config import DIR

# Công cụ làm video/tạo nội dung đa phương tiện: TTS tiếng Việt, sinh ảnh AI,
# ghép scene thành video Shorts (ffmpeg), cắt/nối/đổi cỡ/clip...

OUT = os.path.join(DIR, "media")
for d in (OUT, os.path.join(OUT, "audio"), os.path.join(OUT, "images"),
          os.path.join(OUT, "scenes"), os.path.join(OUT, "videos")):
    os.makedirs(d, exist_ok=True)

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
VOICE_VI = "vi-VN-NamMinhNeural"
VOICE_EN = "en-US-ChristopherNeural"
W, H, FPS = 1080, 1920, 30

for f in (FONT, FONT_FALLBACK):
    if os.path.isfile(f):
        FONT = f
        break


def _ff(path, *args, timeout=120):
    if not os.path.exists(path):
        return f"[LOI] File không tồn tại: {path}"
    try:
        p = subprocess.run(["ffmpeg", "-y", "-i", path, *args],
                           capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return f"[LOI] ffmpeg: {p.stderr[-300:]}"
        return "OK"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def dur(path):
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", path], timeout=20).decode().strip()
        return float(out)
    except Exception:
        return 0.0


def _fps(s):
    """Parse '30000/1001' → float, an toàn (trước đây dùng eval)."""
    try:
        num, _, den = str(s).partition("/")
        den = float(den) if den else 1.0
        return float(num) / den if den else 0.0
    except Exception:
        return 0.0


def probe(path):
    if not os.path.exists(path):
        return f"[LOI] File không tồn tại: {path}"
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", path], timeout=20)
        d = json.loads(out)
        fmt = d.get("format", {})
        v = a = None
        for s in d.get("streams", []):
            if s.get("codec_type") == "video" and v is None:
                v = s
            elif s.get("codec_type") == "audio" and a is None:
                a = s
        info = {"file": path, "duration": float(fmt.get("duration", 0)),
                "size_bytes": fmt.get("size", 0), "format": fmt.get("format_name", "")}
        if v:
            info["video"] = {"codec": v.get("codec_name"), "width": v.get("width"),
                             "height": v.get("height"), "fps": _fps(v.get("r_frame_rate")) if v.get("r_frame_rate") else None}
        if a:
            info["audio"] = {"codec": a.get("codec_name"), "sample_rate": a.get("sample_rate")}
        return json.dumps(info, ensure_ascii=False, indent=1)
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def wrap(text, limit):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= limit:
            cur = (cur + " " + w) if cur else w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def esc(t):
    return (t.replace("\\", "\\\\").replace(":", "\\:")
            .replace("'", "\\'").replace("%", "\\%"))


def valid_audio(path):
    if not os.path.exists(path) or os.path.getsize(path) < 500:
        return False
    try:
        return dur(path) > 0
    except Exception:
        return False


def tts_fallback(text, path):
    try:
        from gtts import gTTS
        t = gTTS(text=text, lang="vi", slow=False)
        t.save(path)
        return valid_audio(path)
    except Exception:
        return False


def tts(text, path, voice="", rate="-10%"):
    text = (text or "").strip()
    if not text:
        return "[LOI] text rỗng"
    voice = voice or VOICE_VI
    try:
        asyncio.run(_tts_async(text, path, voice, rate))
    except Exception as e:
        if not tts_fallback(text, path):
            return f"[LOI] TTS thất bại ({type(e).__name__}: {e})"
    if not valid_audio(path):
        return "[LOI] TTS tạo file lỗi"
    return f"OK {path} ({dur(path):.1f}s)"


async def _tts_async(text, path, voice, rate):
    import edge_tts
    for attempt in range(3):
        try:
            comm = edge_tts.Communicate(text, voice, rate=rate)
            await comm.save(path)
            if valid_audio(path):
                await asyncio.sleep(1.5)
                return
        except Exception:
            await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError("edge-tts fail")


def media_tts(text, voice="", out="", rate="-10%"):
    name = out or re.sub(r"[^\w]+", "_", (text or "")[:30]).strip("_") or "tts"
    path = os.path.join(OUT, "audio", f"{name}.mp3")
    return tts(text, path, voice=voice, rate=str(rate))


def media_image(prompt, out="", width=W, height=H):
    prompt = (prompt or "").strip()
    if not prompt:
        return "[LOI] prompt rỗng"
    name = out or re.sub(r"[^\w]+", "_", prompt[:30]).strip("_") or "img"
    path = os.path.join(OUT, "images", f"{name}.jpg")
    url = ("https://image.pollinations.ai/prompt/" +
           urllib.parse.quote(prompt) +
           f"?width={width}&height={height}&nologo=true&seed={random.randint(1, 999999)}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=90) as r, open(path, "wb") as f:
            f.write(r.read())
        if os.path.getsize(path) < 15000:
            raise ValueError("ảnh lỗi")
        return f"OK {path} ({os.path.getsize(path)//1024} KB)"
    except Exception as e:
        try:
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                f"gradients=s={width}x{height}:c0=0x1a2a6c:c1=0xb21f1f:c2=0xfdbb2d:n=3",
                "-frames:v", "1", path], check=True, capture_output=True, timeout=120)
            return f"Fallback gradient: {path} (AI: {e})"
        except Exception as e2:
            return f"[LOI] {type(e2).__name__}: {e2}"


def _drawtext(text, fontsize=70, y="h*0.6"):
    return (f"drawtext=fontfile={FONT}:text='{esc(text)}':fontcolor=white:"
            f"fontsize={fontsize}:line_spacing=16:borderw=8:bordercolor=black@0.85:"
            f"x=(w-text_w)/2:y={y}")


def media_scene(image, audio, out="", text="", fontsize=0, zoom=0.0012):
    if not os.path.exists(image) or not os.path.exists(audio):
        return "[LOI] cần image + audio cùng tồn tại"
    name = out or os.path.splitext(os.path.basename(image))[0]
    path = os.path.join(OUT, "scenes", os.path.basename(name).replace(".mp4", "") + ".mp4")
    d = dur(audio) + 0.4
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"zoompan=z='min(zoom+{zoom},1.12)':d={int(FPS * d)}:s={W}x{H}:fps={FPS}"
    )
    if text:
        vf += "," + _drawtext(text, fontsize or 70)
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", image, "-i", audio,
            "-filter_complex", f"[0:v]{vf}[v]",
            "-map", "[v]", "-map", "1:a", "-t", f"{d:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-r", str(FPS),
            path], check=True, capture_output=True, timeout=240)
        return f"OK {path} ({dur(path):.1f}s)"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def media_slideshow(cfg):
    """Ghép nhiều scene thành video hoàn chỉnh.
    cfg: {"scenes": [{"text":.., "prompt":..}], "out":"ten"} — tự tạo ảnh + tts + scene."""
    try:
        cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
    except Exception as e:
        return f"[LOI] cfg không phải JSON: {e}"
    scenes = cfg.get("scenes") or []
    if not scenes:
        return "[LOI] cần scenes (list các phân cảnh)"
    name = cfg.get("out") or time_tag()
    scene_paths = []
    report = []
    for i, s in enumerate(scenes):
        tag = f"{name}_{i:02d}"
        st = s.get("text", "")
        sp = os.path.join(OUT, "audio", tag + ".mp3")
        ip = os.path.join(OUT, "images", tag + ".jpg")
        vp = os.path.join(OUT, "scenes", tag + ".mp4")
        if not os.path.isdir(os.path.dirname(sp)):
            os.makedirs(os.path.dirname(sp), exist_ok=True)
        if not valid_audio(sp):
            tts(st, sp)
        if not os.path.exists(ip):
            media_image(s.get("prompt", st), tag)
        r = media_scene(ip, sp, vp, st)
        if r.startswith("[LOI]"):
            return r
        scene_paths.append(vp)
        report.append(f"  scene {i}: {r}")
    final = media_concat(scene_paths, name)
    dl = dur(final) if final and os.path.exists(final) else 0
    return f"Đã làm {len(scenes)} scene:\n" + "\n".join(report) + f"\nFINAL: {final} ({dl:.0f}s)"


def time_tag():
    return __import__("time").strftime("%y%m%d-%H%M%S")


def media_concat(files, out="", copy=True):
    lst = os.path.join(OUT, f"concat_{time_tag()}.txt")
    if isinstance(files, (list, tuple)):
        missing = [f for f in files if not os.path.exists(f)]
        if missing:
            return f"[LOI] thiếu file: {missing}"
        with open(lst, "w", encoding="utf-8") as f:
            for p in files:
                f.write(f"file '{os.path.abspath(p)}'\n")
    elif files.endswith(".txt"):
        lst = files
    else:
        return "[LOI] cần danh sách file hoặc file list .txt"
    name = out or time_tag()
    final = os.path.join(OUT, "videos", f"{name}.mp4")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
           "-c", "copy" if copy else "libx264", final]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return f"OK {final} ({dur(final):.1f}s)"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def media_trim(video, start, length, out=""):
    name = out or os.path.splitext(os.path.basename(video))[0] + "_trim"
    path = os.path.join(OUT, "videos", f"{name}.mp4")
    r = _ff(video, "-ss", str(start), "-t", str(length),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", path)
    return f"{r} => {path} ({dur(path):.1f}s)" if r == "OK" else r


def media_scale(video, width=720, height=1280, out=""):
    name = out or os.path.splitext(os.path.basename(video))[0] + f"_{width}x{height}"
    path = os.path.join(OUT, "videos", f"{name}.mp4")
    r = _ff(video, "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", path)
    return f"{r} => {path} ({dur(path):.1f}s)" if r == "OK" else r


def media_to_gif(video, fps=12, scale=480, out=""):
    name = out or os.path.splitext(os.path.basename(video))[0]
    path = os.path.join(OUT, "videos", f"{name}.gif")
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", video, "-vf",
            f"fps={fps},scale={scale}:-1:flags=lanczos", path],
            check=True, capture_output=True, timeout=180)
        return f"OK {path} ({os.path.getsize(path)//1024} KB)"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def media_overlay_text(video_or_img, text, out="", x="(w-text_w)/2", y="h*0.8", fontsize=60):
    src = video_or_img
    name = out or os.path.splitext(os.path.basename(src))[0] + "_text"
    ext = os.path.splitext(src)[1].lower()
    is_img = ext in (".jpg", ".jpeg", ".png")
    dest = os.path.join(OUT, "videos" if not is_img else "images", f"{name}{'.png' if is_img else '.mp4'}")
    vf = _drawtext(text, fontsize, y)
    if is_img:
        r = _ff(src, "-vf", vf, "-frames:v", "1", dest)
        return f"{r} => {dest}" if r == "OK" else r
    r = _ff(src, "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "copy", "-acodec", "aac", dest)
    return f"{r} => {dest} ({dur(dest):.1f}s)" if r == "OK" else r


def media_extract_audio(video, out=""):
    name = out or os.path.splitext(os.path.basename(video))[0]
    path = os.path.join(OUT, "audio", f"{name}.mp3")
    r = _ff(video, "-vn", "-c:a", "libmp3lame", "-q:a", "4", path)
    return f"{r} => {path} ({dur(path):.1f}s)" if r == "OK" else r


def media_status():
    lines = ["ffmpeg: " + ("OK" if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0 else "THIẾU"),
             "edge-tts: " + ("OK" if __import__("importlib.util").util.find_spec("edge_tts") else "THIẾU (pip install edge-tts)"),
             "gTTS: " + ("OK" if __import__("importlib.util").util.find_spec("gtts") else "THIẾU"),
             f"Output: {OUT}"]
    return "\n".join(lines)


TOOLS = [
    Tool("media_status", "Kiểm tra công cụ đa phương tiện (ffmpeg, edge-tts, gTTS).",
         schema({}), media_status),
    Tool("media_tts", "Tạo audio TTS tiếng Việt (edge-tts, fallback gTTS). Trả về đường dẫn mp3.",
         schema({"text": {"type": "string", "description": "nội dung đọc"},
                 "voice": {"type": "string", "description": "tuỳ chọn, mặc định vi-VN-NamMinhNeural"},
                 "out": {"type": "string", "description": "tên file (không cần .mp3)"},
                 "rate": {"type": "string", "description": "tốc độ đọc, mặc định -10%"}}), media_tts),
    Tool("media_image", "Sinh ảnh bằng AI (pollinations.ai), fallback gradient.",
         schema({"prompt": {"type": "string"}, "out": {"type": "string"},
                 "width": {"type": "integer", "description": "mặc định 1080"},
                 "height": {"type": "integer", "description": "mặc định 1920"}}), media_image),
    Tool("media_scene", "Ghép 1 ảnh + 1 audio thành 1 phân cảnh video (zoom-pan + chữ nổi).",
         schema({"image": {"type": "string"}, "audio": {"type": "string"},
                 "text": {"type": "string", "description": "chữ hiển thị trên video"},
                 "out": {"type": "string"}}), media_scene),
    Tool("media_slideshow", "Tự động làm video Shorts từ danh sách phân cảnh: mỗi scene = TTS + ảnh AI + ghép. cfg JSON: {\"scenes\":[{\"text\":..,\"prompt\":..}],\"out\":\"ten\"}",
         schema({"cfg": {"type": "string", "description": "JSON cấu hình"}}), media_slideshow),
    Tool("media_concat", "Ghép nhiều video thành 1 (danh sách path hoặc file .txt).",
         schema({"files": {"type": "array", "items": {"type": "string"}},
                 "out": {"type": "string"}}), media_concat),
    Tool("media_info", "Probe thông tin video/audio (độ dài, codec, phân giải, fps).",
         schema({"path": {"type": "string"}}), probe),
    Tool("media_trim", "Cắt trích đoạn video.",
         schema({"video": {"type": "string"}, "start": {"type": "number", "description": "giây"},
                 "length": {"type": "number", "description": "giây"}, "out": {"type": "string"}}), media_trim),
    Tool("media_scale", "Đổi cỡ video (thường để 9:16 Shorts).",
         schema({"video": {"type": "string"}, "width": {"type": "integer", "description": "mặc định 720"},
                 "height": {"type": "integer", "description": "mặc định 1280"}, "out": {"type": "string"}}), media_scale),
    Tool("media_to_gif", "Chuyển video thành GIF.",
         schema({"video": {"type": "string"}, "fps": {"type": "integer", "description": "mặc định 12"},
                 "scale": {"type": "integer", "description": "chiều ngang, mặc định 480"}, "out": {"type": "string"}}), media_to_gif),
    Tool("media_overlay_text", "Chèn chữ lên ảnh hoặc video.",
         schema({"video_or_img": {"type": "string"}, "text": {"type": "string"}, "out": {"type": "string"},
                 "fontsize": {"type": "integer", "description": "mặc định 60"}}), media_overlay_text),
    Tool("media_extract_audio", "Tách audio từ video sang mp3.",
         schema({"video": {"type": "string"}, "out": {"type": "string"}}), media_extract_audio),
]

if __name__ == "__main__":
    Server(TOOLS, "media_tools", "0.1.0").serve(sys.stdin, sys.stdout)