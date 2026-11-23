"""Read-only collectors for agent venues added after the 2026-11-23 probe (see README: venues).

Same contract as venues.py: collect() -> list of post dicts (venue, post_id, thread, author, author_meta, body, ts, url).
No credentials, no POSTs. Each collector keeps a high-water mark in store state and caps pages per run, so the first
run backfills a bounded window and later runs are incremental. robots.txt was checked for every endpoint used.
"""
import asyncio
import datetime as dt
import json
import re
import urllib.parse

from .util import CFG, get_json, log

PAGES = CFG.get("new_venue_pages", 30)        # per run; first runs backfill this many pages, then go incremental
THREADS = CFG.get("new_venue_threads", 150)    # reply trees re-read per run (only where the reply count changed)


def iso(v):
    """ISO-8601 Z from epoch s / ms, 'YYYY-MM-DD HH:MM:SS' or ISO with or without zone."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        x = float(v)
        x = x / 1000 if x > 1e12 else x
        return dt.datetime.fromtimestamp(x, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(v).strip().replace(" ", "T")
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def P(venue, pid, thread, author, body, ts, url, parent=None, **meta):
    m = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
    if parent not in (None, "", 0):
        m["parent_id"] = str(parent)
    return dict(venue=venue, post_id=str(pid), thread=str(thread), author=(author or "anon")[:80], author_meta=m,
                body=body or "", ts=iso(ts), url=url)


class _Base:
    name = "?"
    can_post = False

    def __init__(self, store, creds):
        self.s, self.creds = store, creds

    def post(self, *a, **k):
        return False, "posting not supported"

    def _counts(self, key):
        return self.s.get(key, {}) or {}

    def _save_counts(self, key, d):
        self.s.set(key, dict(list(d.items())[-8000:]))


class Tantive(_Base):
    """tantive.space: one chronological stream of every message, replies included."""
    name = "tantive"

    def collect(self):
        since = self.s.get("tantive_cursor", "0")
        url = f"https://tantive.space/api/updates?since={urllib.parse.quote(str(since))}&limit=100&full=1"
        out = []
        for _ in range(PAGES * 2):
            d = get_json(url) or {}
            for m in d.get("data") or []:
                if m.get("removed"):
                    continue
                body = ((m.get("title") or "") + "\n\n" + (m.get("body") or "")).strip()
                out.append(P("tantive", m["id"], m.get("root_id") or m["id"], m.get("author"), body, m.get("created_at"),
                             f"https://tantive.space/t/{m.get('root_id') or m['id']}", parent=m.get("reply_to"),
                             room=m.get("room"), signed=m.get("signature_status"), agent_id=m.get("agent_id")))
            if d.get("cursor") is not None:
                since = d["cursor"]
            if not d.get("has_more") or not d.get("next"):
                break
            url = d["next"] if d["next"].startswith("http") else "https://tantive.space" + d["next"]
        self.s.set("tantive_cursor", since)
        return out


class Waystation(_Base):
    """waystationagents.com: channel lists hold roots; replies come from each root's thread."""
    name = "waystation"
    B = "https://waystationagents.com/api"

    def _msg(self, m, root):
        body = ((m.get("title") or "") + "\n\n" + (m.get("body") or "")).strip()
        who = m.get("agent") if isinstance(m.get("agent"), str) else (m.get("agent") or {}).get("name") or m.get("agentId")
        return P("waystation", m["id"], root, who, body, m.get("createdAt"), f"https://waystationagents.com/m/{root}",
                 parent=m.get("parentId"), channel=m.get("channel"), kind=m.get("kind"), tier=m.get("identityTier"),
                 agent_id=m.get("agentId"))

    def collect(self):
        chans = ((get_json(f"{self.B}/channels") or {}).get("board_content") or {}).get("channels") or []
        last = self._counts("waystation_chan_last")
        counts = self._counts("waystation_reply_counts")
        out, todo = [], []
        for c in chans:
            name = c.get("name") if isinstance(c, dict) else c
            if not name or (isinstance(c, dict) and c.get("lastPostAt") and last.get(name) == c.get("lastPostAt")):
                continue
            before, newest = None, None
            for _ in range(PAGES):
                q = f"{self.B}/messages?channel={urllib.parse.quote(name)}&fields=full" + (f"&before={urllib.parse.quote(before)}" if before else "")
                bc = (get_json(q) or {}).get("board_content") or {}
                ms = bc.get("messages") or []
                stop = False
                for m in ms:
                    newest = newest or m.get("createdAt")
                    if self.s.c.execute("select 1 from posts where uid=?", (f"waystation:{m['id']}",)).fetchone() and counts.get(m["id"]) == m.get("replyCount"):
                        stop = True
                        continue
                    out.append(self._msg(m, m["id"]))
                    if (m.get("replyCount") or 0) and counts.get(m["id"]) != m.get("replyCount"):
                        todo.append((m["id"], m.get("replyCount")))
                before = (bc.get("page") or {}).get("next_before")
                if stop or not ms or not before:
                    break
            if isinstance(c, dict) and c.get("lastPostAt"):
                last[name] = c["lastPostAt"]
        for mid, n in todo[:THREADS]:
            off = 0
            for _ in range(10):
                bc = (get_json(f"{self.B}/messages/{mid}/thread?offset={off}") or {}).get("board_content") or {}
                ms = bc.get("messages") or []
                out += [self._msg(m, mid) for m in ms if m.get("id") != mid]
                off += len(ms)
                if len(ms) < ((bc.get("page") or {}).get("limit") or 200):
                    break
            counts[mid] = n
        self.s.set("waystation_chan_last", last)
        self._save_counts("waystation_reply_counts", counts)
        return out


class SwarmMemo(_Base):
    """swarmmemo.com: global message stream, newest first; page back with older_cursor to the last seen sequence.
    Only /api/ is read: robots disallows /w/ (where a GET publishes), /call/ and /v1/."""
    name = "swarmmemo"

    def collect(self):
        hw = int(self.s.get("swarmmemo_seq", 0) or 0)
        url, top, out = "https://swarmmemo.com/api/messages?sort=new&limit=100", hw, []
        for _ in range(PAGES * 8):          # pages hold only the ~30% of sequence numbers that are public messages
            d = get_json(url) or {}
            ms = d.get("messages") or []
            for m in ms:
                seq = int(m.get("sequence") or 0)
                top = max(top, seq)
                if seq and seq <= hw:
                    continue
                room, page = m.get("room") or "", m.get("page") or ""
                out.append(P("swarmmemo", m["id"], f"{room}/{page}", m.get("handle") or (m.get("author") or "")[:16], m.get("text"),
                             m.get("created_at"), f"https://swarmmemo.com/r/{urllib.parse.quote(room)}", parent=m.get("reply_to"),
                             room=room, pubkey=m.get("author")))
            oc = d.get("older_cursor")
            if not ms or not oc or not (d.get("data") or {}).get("has_more", True) or any(int(m.get("sequence") or 0) <= hw for m in ms):
                break
            url = f"https://swarmmemo.com/api/messages?sort=new&limit=100&older={urllib.parse.quote(str(oc))}"
        self.s.set("swarmmemo_seq", top)
        return out


class Clawprint(_Base):
    """clawprint.org: agent blog posts (full body per new post) and their comments when the count changed."""
    name = "clawprint"

    def collect(self):
        counts, out = self._counts("clawprint_counts"), []
        budget = THREADS // 3
        for k in range(max(2, PAGES // 4)):     # one full-body fetch per new post: keep runs short
            ps = (get_json(f"https://clawprint.org/api/posts?limit=50&offset={50 * k}") or {}).get("posts") or []
            fresh = 0
            for p in ps:
                sl, pid = p.get("slug"), p.get("id")
                known = self.s.c.execute("select 1 from posts where uid=?", (f"clawprint:{pid}",)).fetchone()
                if not known:
                    fresh += 1
                    full = get_json(f"https://clawprint.org/api/posts/{urllib.parse.quote(str(sl))}") or {}
                    full = full.get("post") or full
                    body = ((p.get("title") or "") + "\n\n" + (full.get("content") or full.get("body") or p.get("preview") or "")).strip()
                    out.append(P("clawprint", pid, pid, p.get("author_name"), body, p.get("created_at"),
                                 f"https://clawprint.org/p/{sl}", tags=p.get("tags")))
                n = p.get("comment_count") or 0
                if n and counts.get(str(pid)) != n and budget > 0:
                    budget -= 1
                    for off in range(0, min(n, 1000), 100):
                        cs = (get_json(f"https://clawprint.org/api/posts/{urllib.parse.quote(str(sl))}/comments?limit=100&offset={off}") or {}).get("comments") or []
                        out += [P("clawprint", f"c{c['id']}", pid, c.get("author_name"), c.get("content"), c.get("created_at"),
                                  f"https://clawprint.org/p/{sl}", parent=c.get("parent_id")) for c in cs]
                        if len(cs) < 100:
                            break
                    counts[str(pid)] = n
            if not ps or (k and not fresh and budget <= 0):
                break
        self._save_counts("clawprint_counts", counts)
        return out


def _tree(o, depth=0):
    for r in (o if isinstance(o, list) else o.get("replies") or o.get("children") or []):
        yield r
        if depth < 30:
            yield from _tree(r.get("replies") or r.get("children") or [], depth + 1)


class Abund(_Base):
    """abund.ai: posts (newest first, page=) and their reply trees when reply_count changed."""
    name = "abund"
    B = "https://api.abund.ai/api/v1"

    def _p(self, r, root):
        a = r.get("agent") or {}
        return P("abund", r["id"], root, a.get("handle"), r.get("content"), r.get("created_at"), f"https://abund.ai/post/{root}",
                 parent=r.get("parent_id") or r.get("reply_to_id"), community=(r.get("community") or {}).get("slug") if isinstance(r.get("community"), dict) else r.get("community"),
                 claimed=a.get("is_claimed"))

    def collect(self):
        counts, out, todo = self._counts("abund_counts"), [], []
        for page in range(1, PAGES + 1):
            ps = (get_json(f"{self.B}/posts?sort=new&limit=50&page={page}") or {}).get("posts") or []
            fresh = 0
            for p in ps:
                if not self.s.c.execute("select 1 from posts where uid=?", (f"abund:{p['id']}",)).fetchone():
                    fresh += 1
                    out.append(self._p(p, p["id"]))
                if (p.get("reply_count") or 0) and counts.get(p["id"]) != p.get("reply_count"):
                    todo.append((p["id"], p["reply_count"]))
            if not ps or not fresh:
                break
        for pid, n in todo[:THREADS]:
            d = get_json(f"{self.B}/posts/{pid}/replies?max_depth=10") or {}
            for r in _tree(d.get("replies") or []):
                if r.get("id"):
                    q = self._p(r, pid)
                    q["author_meta"].setdefault("parent_id", str(pid))
                    out.append(q)
            counts[pid] = n
        self._save_counts("abund_counts", counts)
        return out


class MusedIn(_Base):
    """musedin.com: the public feed of posts and replies (parent_id), authors looked up in the returned muses map."""
    name = "musedin"

    def collect(self):
        hw = self.s.get("musedin_last", 0) or 0
        url, top, out = "https://musedin.com/api/feed?kind=posts&limit=50", hw, []
        roots = {}
        for _ in range(PAGES):
            d = get_json(url) or {}
            items, muses = d.get("items") or [], d.get("muses") or {}
            for m in items:
                t = m.get("created_at") or 0
                top = max(top, t)
                if t and t <= hw:
                    continue
                mu = muses.get(str(m.get("muse_id"))) or muses.get(m.get("muse_id")) or {}
                who = mu.get("handle") or mu.get("name") or str(m.get("muse_id"))
                par = m.get("parent_id")
                roots[str(m["id"])] = roots.get(str(par), str(par)) if par else str(m["id"])
                out.append(P("musedin", m["id"], roots[str(m["id"])], who, m.get("text"), t,
                             f"https://musedin.com/p/{m['id']}", parent=par, board=m.get("board")))
            nxt = d.get("next")
            if not items or not nxt or any((m.get("created_at") or 0) <= hw for m in items):
                break
            url = f"https://musedin.com/api/feed?kind=posts&limit=50&before={urllib.parse.quote(str(nxt))}"
        self.s.set("musedin_last", top)
        return out


class Taskmarket(_Base):
    """taskmarket.dev: posted tasks with reward and status (robots: use=reference). One post per task; the
    status and submission count are refreshed in author_meta when they change."""
    name = "taskmarket"

    def collect(self):
        url, out = "https://api.taskmarket.dev/api/tasks?sort=newest&limit=50", []
        for _ in range(PAGES):
            d = get_json(url) or {}
            ts = d.get("tasks") or []
            for t in ts:
                body = f"{t.get('description') or ''}\n\nreward: {t.get('reward')}"
                p = P("taskmarket", t["id"], t["id"], t.get("requesterDisplayName"), body, t.get("createdAt"),
                      f"https://taskmarket.dev/tasks/{t.get('referenceCode') or t['id']}", status=t.get("status"),
                      phase=t.get("phase"), submissions=t.get("submissionCount"), requester_type=t.get("requesterActorType"),
                      reward=t.get("reward"))
                out.append(p)
            if not ts or not d.get("hasMore") or not d.get("nextCursor"):
                break
            if self.s.c.execute("select 1 from posts where uid=?", (f"taskmarket:{ts[-1]['id']}",)).fetchone():
                break
            url = f"https://api.taskmarket.dev/api/tasks?sort=newest&limit=50&cursor={urllib.parse.quote(str(d['nextCursor']))}"
        return out


class LatentReview(_Base):
    """thelatentreview.com: a small agent-run review journal (JSON Feed)."""
    name = "latentreview"

    def collect(self):
        d = get_json("https://thelatentreview.com/feed.json") or {}
        out = []
        for it in d.get("items") or []:
            who = ", ".join(a.get("name", "") for a in it.get("authors") or []) or "thelatentreview"
            txt = re.sub(r"<[^>]+>", " ", it.get("content_html") or "")
            out.append(P("latentreview", it["id"], it["id"], who, ((it.get("title") or "") + "\n\n" + re.sub(r"\s+", " ", txt)).strip(),
                         it.get("date_published"), it.get("url")))
        return out


# ------------------------------------------------------------------------------------------------ nostr
BECH = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
RELAYS = CFG.get("nostr_relays", ["wss://relay.damus.io", "wss://nos.lol", "wss://relay.primal.net"])


def _bech_hex(s):
    data = s.rsplit("1", 1)[1]
    acc = bits = 0
    out = []
    for ch in data[:-6]:
        acc = (acc << 5) | BECH.index(ch)
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 255)
    return bytes(out).hex()


class Nostr(_Base):
    """Nostr kind-1 notes of npubs that agents in the census cite, plus replies to them, from three public relays.
    Needs the `websockets` package; skipped (with a log line) when it is missing."""
    name = "nostr"

    def _seeds(self):
        keys = set(self.s.get("nostr_authors", []) or [])
        for (b,) in self.s.c.execute("select body from posts where body like '%npub1%'"):
            for n in re.findall(r"\bnpub1[02-9ac-hj-np-z]{58}\b", b or ""):
                try:
                    keys.add(_bech_hex(n))
                except ValueError:
                    pass
        return sorted(k for k in keys if len(k) == 64)

    async def _req(self, ws, flt, cap=2000):
        sid = f"census{abs(hash(json.dumps(flt))) % 10**6}"
        await ws.send(json.dumps(["REQ", sid, flt]))
        evs = []
        while len(evs) < cap:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m[0] == "EVENT":
                evs.append(m[2])
            else:
                break
        await ws.send(json.dumps(["CLOSE", sid]))
        return evs

    async def _relay(self, url, authors, since):
        import websockets
        async with websockets.connect(url, open_timeout=10, user_agent_header=CFG["user_agent"], max_size=2**22) as ws:
            notes = await self._req(ws, {"kinds": [1], "authors": authors, "since": since, "limit": 1000})
            ids = [e["id"] for e in notes]
            reps = []
            for i in range(0, len(ids), 100):
                reps += await self._req(ws, {"kinds": [1], "#e": ids[i:i + 100], "limit": 1000})
            return notes + reps

    def collect(self):
        try:
            import websockets  # noqa: F401
        except ImportError:
            log("[nostr] websockets not installed, skipping")
            return []
        authors = self._seeds()
        if not authors:
            return []
        since = int(self.s.get("nostr_since", 0) or 0)
        evs = {}
        for r in RELAYS:
            try:
                for e in asyncio.run(self._relay(r, authors, since)):
                    evs[e["id"]] = e
            except Exception as ex:  # noqa: BLE001
                log(f"[nostr] {r}: {type(ex).__name__} {str(ex)[:80]}")
        out = []
        for e in evs.values():
            etags = [t[1] for t in e.get("tags", []) if len(t) > 1 and t[0] == "e"]
            root = next((t[1] for t in e.get("tags", []) if len(t) > 3 and t[0] == "e" and t[3] == "root"), etags[0] if etags else e["id"])
            reply = next((t[1] for t in e.get("tags", []) if len(t) > 3 and t[0] == "e" and t[3] == "reply"), etags[-1] if etags else None)
            out.append(P("nostr", e["id"], root, e["pubkey"][:16], e.get("content"), e.get("created_at"),
                         f"https://njump.me/{e['id']}", parent=reply, pubkey=e["pubkey"]))
        if evs:
            self.s.set("nostr_since", max(e["created_at"] for e in evs.values()) - 3600)
        self.s.set("nostr_authors", authors)
        return out


ALL = [Tantive, Waystation, SwarmMemo, Clawprint, Abund, MusedIn, Taskmarket, LatentReview, Nostr]
