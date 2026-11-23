import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
UA = CFG["user_agent"]
_last_hit: dict[str, float] = {}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def http(method, url, body=None, headers=None, timeout=30, polite=1.0):
    """Return (status, text). Never raises for HTTP/network errors (status 0 = network)."""
    host = urllib.parse.urlparse(url).netloc
    wait = polite - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    data = None
    if body is not None:
        data = json.dumps(body).encode() if not isinstance(body, (bytes, str)) else (
            body.encode() if isinstance(body, str) else body)
        h["Content-Type"] = "application/json" if not isinstance(body, (bytes, str)) else "text/plain"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(30_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(200_000).decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def get_json(url, headers=None, timeout=30, retries=2):
    st, txt = http("GET", url, headers=headers, timeout=timeout)
    for i in range(retries):        # network hiccups (connection reset, timeout) are common on small venues
        if st not in (0, 429, 502, 503, 504):
            break
        time.sleep(3 * (i + 1))
        st, txt = http("GET", url, headers=headers, timeout=timeout)
    if st != 200:
        return None
    try:
        return json.loads(txt)
    except ValueError:
        return None


def post_json(url, body, headers=None, timeout=30):
    st, txt = http("POST", url, body=body, headers=headers, timeout=timeout)
    try:
        return st, json.loads(txt)
    except ValueError:
        return st, {"_text": txt[:500]}


URL_RE = re.compile(r"https?://([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})(/[^\s\"'<>)\]]*)?")
BARE_RE = re.compile(r"(?<![@\w.-])((?:[a-z0-9-]+\.)+(?:ai|dev|com|net|org|io|club|app|xyz|live|chat|sh|cc|me|so|space|world|zone|network|social))\b")


def domains_in(text: str) -> set[str]:
    out = {m.group(1).lower() for m in URL_RE.finditer(text or "")}
    out |= {m.group(1).lower() for m in BARE_RE.finditer((text or "").lower())}
    return {d.strip(".") for d in out if len(d) < 80}


def load_creds() -> dict:
    """Venue credentials: env CENSUS_CREDS (JSON) or ~/.config/agent-census/creds.json."""
    if os.environ.get("CENSUS_CREDS"):
        return json.loads(os.environ["CENSUS_CREDS"])
    p = os.path.expanduser("~/.config/agent-census/creds.json")
    return json.load(open(p)) if os.path.exists(p) else {}
