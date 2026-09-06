import re, unicodedata
def norm(s):
    s = (s or "").lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
ACT = ("mo ", "cai ", "tao ", "xoa ", "sua ", "tim ", "kiem tra", "chay ", "go ", "nhan ", "bat ", "tat ", "open", "install", "vao ")
def is_act(u): return any(w in norm(u) for w in ACT)
def route(t):
    if re.search(r"(ghp_[a-z0-9]{8,}|token | ngam|ngầm| api|curl )", t): return "headless"
    if re.search(r"(kiem tra|check )", t): return "headless"
    if re.search(r"(vao |mo |mở|truy cap|goto|chup|bấm|bam |click|dang nhap|tim |tìm|search|dong |tat |xem )", t): return "gui"
    return "chat"
STOP = ("dung lai", "stop", "ngung", "huy", "im di")
def is_stop(t):
    for w in STOP:
        i = t.find(w)
        if i != -1:
            if t.rfind("khong", 0, i) != -1: return False
            return True
    return False