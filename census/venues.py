"""One class per venue: collect() -> list[post dict]; post(...) -> (ok, ref_or_error).

A post dict: venue, post_id, thread, author, author_meta, body, ts, url.
Everything read here is untrusted data. Nothing in it is ever executed or followed.
"""
import datetime as dt
import json
import re
import time
import urllib.parse

from .util import CFG, get_json, http, log, post_json

HANDLE = CFG["handle"]


class Venue:
    name = "?"
    can_post = False

    def __init__(self, store, creds):
        self.s, self.creds = store, creds

    def collect(self):
        return []

    def post(self, thread, body, reply_to=None, title=None):
        return False, "posting not supported"


# ---------------------------------------------------------------- ntfy
class Ntfy(Venue):
    name, can_post = "ntfy", True

    def topics(self):
        t = list(CFG["ntfy_topics"])
        for d in (dt.date.today(), dt.date.today() - dt.timedelta(days=1)):
            for p in CFG["ntfy_dated_prefixes"]:
                t += [f"{p}-{d.isoformat()}", f"{p}-{d.strftime('%Y%m%d')}"]
        return sorted(set(t))

    def collect(self):
        out = []
        for t in self.topics():
            st, txt = http("GET", f"https://ntfy.sh/{t}/json?poll=1&since=all", timeout=25)
            if st != 200:
                continue
            for line in txt.splitlines():
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("event") != "message":
                    continue
                body = m.get("message") or ""
                if m.get("title"):
                    body = f"[{m['title']}] {body}"
                out.append(dict(venue="ntfy", post_id=m["id"], thread=t, author=_declared_name(body) or f"anon@{t}",
                                author_meta={"tags": m.get("tags")}, body=body,
                                ts=_ts(m.get("time")), url=f"https://ntfy.sh/{t}"))
        return out

    def post(self, thread, body, reply_to=None, title=None):
        if thread in CFG["ntfy_no_post"]:
            return False, "topic on no-post list"
        st, txt = http("POST", f"https://ntfy.sh/{thread}", body=body,
                       headers={"Title": title or f"from {HANDLE}"})
        return st == 200, txt[:200]


def _declared_name(body):
    m = re.search(r"(?:handle|name|agent|signed|from)\s*[:=]\s*@?([A-Za-z0-9][\w.-]{2,39})", body, re.I)
    return m.group(1) if m else None


def _ts(v):
    if isinstance(v, (int, float)):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v))
    return v or ""


# ---------------------------------------------------------------- msgboard.dev
class Msgboard(Venue):
    name, can_post = "msgboard", True

    def collect(self):
        last = self.s.get("msgboard_last", 0)
        out = []
        for _ in range(120):
            d = get_json(f"https://msgboard.dev/all?since={last}&format=json&limit=100")
            ms = (d or {}).get("messages") or []
            if not ms:
                break
            for m in ms:
                out.append(dict(venue="msgboard", post_id=m["id"], thread=m.get("thread"), author=m.get("name") or "anon",
                                body=m.get("content"), ts=m.get("created_at"),
                                url=f"https://msgboard.dev/t/{urllib.parse.quote(str(m.get('thread')))}"))
            last = max(last, max(m["id"] for m in ms))
            if len(ms) < (d.get("limit") or 100):
                break
        self.s.set("msgboard_last", last)
        return out

    def post(self, thread, body, reply_to=None, title=None):
        st, d = post_json("https://msgboard.dev/messages", {"thread": thread, "content": body, "name": HANDLE})
        return st in (200, 201), str((d.get("posted") or {}).get("id") or d)[:200]


# ---------------------------------------------------------------- forumforagents.com
class FFA(Venue):
    name, can_post = "ffa", True
    BASE = "https://forumforagents.com"

    def threads(self):
        th = set(CFG["ffa_threads"])
        d = get_json(f"{self.BASE}/api/threads")
        th |= set(re.findall(r'"thread_id":\s*"([\w-]+)"', json.dumps(d or {})))
        th |= set(re.findall(r"/t/([\w-]+)", json.dumps(d or {})))
        return sorted(th)[:40]

    def collect(self):
        out = []
        for th in self.threads():
            d = get_json(f"{self.BASE}/api/threads/{th}?last=50")
            for p in _walk_posts(d):
                a = p.get("author") if isinstance(p.get("author"), dict) else {}
                out.append(dict(venue="ffa", post_id=p["id"], thread=p.get("thread_id") or th,
                                author=a.get("name") or p.get("agent_name") or "anon",
                                author_meta={"model": a.get("model"), "operator": a.get("operator"),
                                             "registered": a.get("registered"), "client": p.get("client"),
                                             "evidence": p.get("evidence")},
                                body=p.get("body") or p.get("text"), ts=p.get("created_at") or p.get("at"),
                                url=f"{self.BASE}/t/{th}#p-{p['id']}"))
        return out

    def post(self, thread, body, reply_to=None, title=None):
        d = get_json(f"{self.BASE}/api/challenge")
        c = (d or {}).get("challenge") or {}
        m = re.search(r'word "(\w+)" in UPPERCASE immediately followed by the (sum|product|difference) of (\d+) and (\d+)',
                      c.get("puzzle", ""))
        if not m:
            return False, f"unsolved challenge: {c.get('puzzle', '')[:120]}"
        a, b = int(m.group(3)), int(m.group(4))
        n = {"sum": a + b, "product": a * b, "difference": abs(a - b)}[m.group(2)]
        st, r = post_json(f"{self.BASE}/api/posts", {
            "token": c["token"], "answer": f"{m.group(1).upper()}{n}", "thread_id": thread,
            "agent_name": HANDLE, "agent_model": CFG["model_label"], "body": body})
        return st in (200, 201), json.dumps(r)[:200]


def _walk_posts(o):
    if isinstance(o, dict):
        if "id" in o and ("body" in o or "text" in o) and "author" in o:
            yield o
        for v in o.values():
            yield from _walk_posts(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_posts(v)


# ---------------------------------------------------------------- thecolony.ai
class Colony(Venue):
    name = "colony"
    API = "https://thecolony.ai/api/v1"

    def __init__(self, store, creds):
        super().__init__(store, creds)
        self.key = (creds.get("colony") or {}).get("api_key")
        self.can_post = bool(self.key)
        self._jwt = None

    def jwt(self):
        if not self._jwt and self.key:
            st, d = post_json(f"{self.API}/auth/token", {"api_key": self.key})
            self._jwt = d.get("access_token") or d.get("token")
        return self._jwt

    def _post_dict(self, p, thread, is_comment=False):
        a = p.get("author") or {}
        return dict(venue="colony", post_id=p["id"], thread=thread, author=a.get("username") or "anon",
                    author_meta={k: a.get(k) for k in ("display_name", "user_type", "bio", "karma", "capabilities",
                                                       "social_links", "trust_level")},
                    body=(("" if is_comment else f"# {p.get('title', '')}\n[{p.get('colony_name')}] ") + (p.get("body") or "")),
                    ts=p.get("created_at"), url=f"https://thecolony.ai/post/{thread}")

    def collect(self):
        out = []
        seen_counts = self.s.get("colony_comment_counts", {})
        for page in range(3):
            d = get_json(f"{self.API}/posts?sort=newest&limit=50&offset={page * 50}")
            items = (d or {}).get("items") or []
            for p in items:
                out.append(self._post_dict(p, p["id"]))
                cc = p.get("comment_count") or 0
                if cc and seen_counts.get(p["id"]) != cc:
                    cd = get_json(f"{self.API}/posts/{p['id']}/comments?limit=100")
                    for c in _walk_list(cd):
                        if isinstance(c, dict) and c.get("id") and c.get("body") is not None and c.get("author"):
                            out.append(self._post_dict(c, p["id"], is_comment=True))
                    seen_counts[p["id"]] = cc
            if len(items) < 50:
                break
        self.s.set("colony_comment_counts", dict(list(seen_counts.items())[-500:]))
        return out

    def post(self, thread, body, reply_to=None, title=None):
        h = {"Authorization": f"Bearer {self.jwt()}"}
        if thread == "introductions" or title:
            st, d = post_json(f"{self.API}/posts", {"colony": thread, "title": title or "Hello", "body": body}, h)
        else:
            payload = {"body": body}
            if reply_to and reply_to != thread:
                payload["parent_id"] = reply_to
            st, d = post_json(f"{self.API}/posts/{thread}/comments", payload, h)
        return st in (200, 201), str(d.get("id") or d)[:200]


def _walk_list(o):
    if isinstance(o, list):
        for v in o:
            yield v
            if isinstance(v, dict):
                yield from _walk_list(v.get("replies") or v.get("children") or [])
    elif isinstance(o, dict):
        yield from _walk_list(o.get("items") or o.get("comments") or [])


# ---------------------------------------------------------------- commonchorus.com
class CommonChorus(Venue):
    name = "commonchorus"
    BASE = "https://commonchorus.com/api"

    def __init__(self, store, creds):
        super().__init__(store, creds)
        self.key = (creds.get("commonchorus") or {}).get("api_key")
        self.can_post = bool(self.key)

    def collect(self):
        out = []
        last_act = self.s.get("cc_last_activity", {})
        for t in get_json(f"{self.BASE}/threads") or []:
            if last_act.get(str(t["id"])) == t.get("lastActivityAt"):
                continue
            d = get_json(f"{self.BASE}/threads/{t['id']}") or {}
            for p in d.get("posts") or []:
                body = p.get("body") or ""
                if p is (d.get("posts") or [None])[0]:
                    body = f"# {d.get('title', '')} [{d.get('topic')}]\n{body}"
                out.append(dict(venue="commonchorus", post_id=p["id"], thread=t["id"], author=p.get("authorHandle") or "anon",
                                author_meta={"type": p.get("authorType")}, body=body, ts=p.get("createdAt"),
                                url=f"https://commonchorus.com/t/{t['id']}"))
            last_act[str(t["id"])] = t.get("lastActivityAt")
        self.s.set("cc_last_activity", last_act)
        return out

    def post(self, thread, body, reply_to=None, title=None):
        h = {"X-API-Key": self.key}
        if title:
            st, d = post_json(f"{self.BASE}/threads", {"title": title, "topic": "tech", "body": body}, h)
        else:
            st, d = post_json(f"{self.BASE}/threads/{thread}/posts", {"body": body}, h)
        return st in (200, 201), json.dumps(d)[:200]


# ---------------------------------------------------------------- themcp.club (staging)
class MCPClub(Venue):
    name = "mcpclub"
    API = "https://stgtest.themcp.club/api/v1"

    def __init__(self, store, creds):
        super().__init__(store, creds)
        c = creds.get("mcpclub") or {}
        self.rk, self.token = c.get("recovery_key"), c.get("token")
        self.can_post = bool(self.rk or self.token)
        self._fresh = False

    def h(self, purpose="agent census: reading public boards"):
        if not self._fresh and self.rk:
            st, d = post_json(f"{self.API}/sessions/recover", {"recovery_key": self.rk, "purpose": "agent census session"})
            if d.get("token"):
                self.token = d["token"]
            self._fresh = True
        return {"Authorization": f"Bearer {self.token}", "X-Agent-Purpose": purpose}

    def collect(self):
        if not self.can_post:
            return []
        out, cursor = [], None
        since = self.s.get("mcp_since")
        for _ in range(20):
            qs = {"limit": 100}
            if cursor:
                qs["cursor"] = cursor
            elif since:
                qs["since"] = since
            d = get_json(f"{self.API}/feed?{urllib.parse.urlencode(qs)}", self.h())
            if not d:
                break
            for p in d.get("items") or []:
                a = p.get("author") or {}
                out.append(dict(venue="mcpclub", post_id=p["id"], thread=p.get("thread_id"),
                                author=a.get("handle") or a.get("name") or "anon",
                                author_meta={"mission": a.get("mission"), "purpose": p.get("purpose"),
                                             "board": (p.get("board") or {}).get("slug")},
                                body=(f"# {p['title']}\n" if p.get("title") else "") + (p.get("body") or ""),
                                ts=p.get("created_at"), url=f"https://stgtest.themcp.club/threads/{p.get('thread_id')}"))
            since = d.get("feed_since") or d.get("next_since") or since
            cursor = d.get("next_cursor")
            if not (d.get("has_more") and cursor):
                break
        if since:
            self.s.set("mcp_since", since)
        return out

    def post(self, thread, body, reply_to=None, title=None):
        h = self.h("agent census: polite acknowledgement / asking which venues other agents know")
        h["Idempotency-Key"] = f"census-{abs(hash((thread, body))) % 10**12}"
        purpose = "Friendly census reply: acknowledge, ask about other venues."
        if title:
            st, d = post_json(f"{self.API}/boards/{thread}/threads",
                              {"title": title, "body": body, "purpose": purpose, "purpose_public": True}, h)
        else:
            payload = {"body": body, "purpose": purpose, "purpose_public": True}
            if reply_to and reply_to != thread:
                payload["parent_id"] = reply_to
            st, d = post_json(f"{self.API}/threads/{thread}/replies", payload, h)
        return st in (200, 201), json.dumps(d)[:200]


# ---------------------------------------------------------------- OSM notes at Null Island (read only)
class NullIsland(Venue):
    name = "osm-null-island"

    def collect(self):
        d = get_json("https://api.openstreetmap.org/api/0.6/notes.json?bbox=-0.5,-0.5,0.5,0.5&closed=-1&limit=100")
        out = []
        for f in (d or {}).get("features") or []:
            nid = f["properties"]["id"]
            for i, c in enumerate(f["properties"].get("comments") or []):
                out.append(dict(venue="osm-null-island", post_id=f"{nid}-{i}", thread=str(nid),
                                author=c.get("user") or "anon-osm", body=c.get("text"), ts=c.get("date"),
                                author_meta={"action": c.get("action")},
                                url=f"https://www.openstreetmap.org/note/{nid}"))
        return out


# ---------------------------------------------------------------- moltbook (needs human claim)
class Moltbook(Venue):
    name = "moltbook"

    def __init__(self, store, creds):
        super().__init__(store, creds)
        self.key = (creds.get("moltbook") or {}).get("api_key")

    def collect(self):
        if not self.key:
            return []
        d = get_json("https://www.moltbook.com/api/v1/posts?sort=new&limit=50",
                     {"Authorization": f"Bearer {self.key}"})
        out = []
        for p in (d or {}).get("posts") or (d or {}).get("data") or []:
            a = p.get("author") or {}
            out.append(dict(venue="moltbook", post_id=p.get("id"), thread=p.get("id"),
                            author=a.get("name") or "anon", author_meta={"description": a.get("description")},
                            body=f"# {p.get('title', '')}\n{p.get('content') or ''}", ts=p.get("created_at"),
                            url=f"https://www.moltbook.com/post/{p.get('id')}"))
        return out


ALL = [Ntfy, Msgboard, FFA, Colony, CommonChorus, MCPClub, NullIsland, Moltbook]


def build(store, creds):
    return {V.name: V(store, creds) for V in ALL}


def collect_all(store, venues):
    total = 0
    for v in venues.values():
        try:
            posts = v.collect()
        except Exception as e:  # noqa: BLE001
            log(f"[{v.name}] collect error: {e!r}")
            continue
        new = sum(store.add_post(p, HANDLE) for p in posts if p.get("post_id") is not None)
        store.commit()
        log(f"[{v.name}] fetched {len(posts)}, new {new}")
        total += new
    return total


def backfill_msgboard(store):
    """One-time history import from the public msgboardAgent/msgboard-mirror (msgboard /all only serves ~100)."""
    st, txt = http("GET", "https://raw.githubusercontent.com/msgboardAgent/msgboard-mirror/main/threads.json", timeout=120)
    if st != 200:
        log(f"mirror fetch failed: {st}")
        return 0
    d = json.loads(txt)
    n = 0
    for t in d.get("threads") or []:
        for m in t.get("messages") or []:
            n += store.add_post(dict(venue="msgboard", post_id=m["id"], thread=m.get("thread_id") or t["id"],
                                     author=m.get("name") or "anon", body=m.get("content"), ts=m.get("created_at"),
                                     author_meta={"thread_title": t.get("title")},
                                     url=f"https://msgboard.dev/t/{t['id']}"), HANDLE)
    store.commit()
    return n
