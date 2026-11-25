"""Export the data behind the public page (site/data/) and a summary copy in data/census.json.

summary.json          totals, venues, agents (profiles, no posts), discovered venues, conversations, highlights
exchanges.json        every reciprocal exchange: participants, metrics, grade, summary, events, recruitment
ex/<id>.json          one exchange in full: transcript, model assessment, web forensics
recruitment.json      every tracked invitation and its outcome, plus per-recruiter funnels
posts/<venue>.json    {author: [[ts, thread, body, url, uid], ...]}, loaded by the page only when needed
browse/<venue>/<n>.json  every post, newest first, 800 per chunk: [uid, ts, author, thread, body, url, [exchange ids]]
"""
import json
import os
from collections import Counter, defaultdict

from . import exchanges as X
from .util import CFG, ROOT, now

SITE = os.path.join(ROOT, "site", "data")
HANDLE = CFG["handle"]


def _j(s):
    try:
        return json.loads(s) if s else None
    except ValueError:
        return None


def build(store):
    os.makedirs(os.path.join(SITE, "posts"), exist_ok=True)
    agents = []
    for a in store.q("select * from agents order by last_seen desc"):
        p = _j(a["profile"]) or {}
        agents.append({"key": a["key"], "venue": a["venue"], "handle": a["handle"], "first": a["first_seen"],
                       "last": a["last_seen"], "n": a["n_posts"], "greeted": bool(a["greeted_at"]),
                       **{k: p.get(k) for k in ("kind", "claimed_model", "claimed_framework", "claimed_operator",
                                                "stated_aims", "behaviors", "risk_flags", "other_venues_mentioned",
                                                "summary", "confidence")}})
    venues = [{"venue": r["venue"], "posts": r["posts"], "authors": r["authors"], "last": r["last"],
               "week": r["week"]} for r in store.q(
        """select venue, count(*) posts, count(distinct author) authors, max(ts) last,
           sum(case when ts > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days') then 1 else 0 end) week
           from posts group by venue order by week desc, posts desc""")]
    found = []
    for r in store.q("select domain, source, first_seen, mentions, verdict from venues where verdict is not null order by mentions desc"):
        v = _j(r["verdict"]) or {}
        if v.get("is_agent_venue") in ("yes", "maybe"):
            found.append({"domain": r["domain"], "mentions": r["mentions"], "first": r["first_seen"],
                          "via": "agent tip" if (r["source"] or "").startswith("tip:") else "mentioned in a post", **v})

    convos = []
    for i in store.q("select * from interactions where dry=0 and result like 'ok%' order by ts desc"):
        ctx = []
        if i["in_reply_to"] and not i["in_reply_to"].startswith("intro:"):
            t = store.q("select author, body, ts, url from posts where uid=?", i["in_reply_to"])
            if t:
                ctx.append({"who": t[0]["author"], "ts": t[0]["ts"], "body": (t[0]["body"] or "")[:3000], "url": t[0]["url"]})
        ctx.append({"who": HANDLE, "ts": i["ts"], "body": i["body"], "own": True})
        for r in store.q("select author, body, ts, url from posts where venue=? and thread=? and ts > ? and own=0 order by ts limit 6",
                         i["venue"], i["thread"], i["ts"]):
            ctx.append({"who": r["author"], "ts": r["ts"], "body": (r["body"] or "")[:3000], "url": r["url"]})
        convos.append({"id": i["id"], "venue": i["venue"], "with": i["target"], "ts": i["ts"],
                       "intro": (i["in_reply_to"] or "").startswith("intro:"), "replies": len(ctx) - (1 if ctx[0].get("own") else 2),
                       "messages": ctx})

    profiled = [a for a in agents if a.get("kind")]
    totals = {"posts": sum(v["posts"] for v in venues), "authors": len(agents), "profiled": len(profiled),
              "venues": len(venues), "found": len(found), "convos": len(convos),
              "kinds": dict(Counter(a["kind"] for a in profiled)),
              "models": Counter((a.get("claimed_model") or "").strip() for a in profiled if a.get("kind") == "llm_agent"
                                and (a.get("claimed_model") or "").strip()).most_common(12)}
    summary = {"generated": now(), "totals": totals, "venues": venues, "agents": agents, "found": found,
               "convos": convos, "highlights": store.get("highlights", [])}

    xs, recr = export_exchanges(store)
    from . import recruits
    json.dump(recruits.export(store), open(os.path.join(SITE, "recruits.json"), "w"), separators=(",", ":"), ensure_ascii=False)
    summary["totals"].update(xs)
    summary["recruitment"] = recr

    by_venue = defaultdict(lambda: defaultdict(list))
    for r in store.q("select uid, venue, author, ts, thread, body, url from posts where own=0 order by ts desc"):
        lst = by_venue[r["venue"]][r["author"]]
        if len(lst) < 150:
            lst.append([r["ts"], r["thread"], (r["body"] or "")[:2500], r["url"], r["uid"]])
    for v, d in by_venue.items():
        json.dump(d, open(os.path.join(SITE, "posts", f"{v}.json"), "w"), separators=(",", ":"))
    export_browse(store)
    json.dump(summary, open(os.path.join(SITE, "summary.json"), "w"), separators=(",", ":"), default=str)
    json.dump(summary, open(os.path.join(ROOT, "data", "census.json"), "w"), indent=1, default=str)
    return summary


def _short(m):
    a = (m or {}).get("authors") or {}
    return {"lat": m.get("latency_median_s"), "fast": m.get("fast_share"), "how": m.get("how"),
            "sched": {k: (v or {}).get("scheduled") for k, v in a.items()},
            "tmpl": {k: (v or {}).get("template") for k, v in a.items()},
            "posts": {k: (v or {}).get("posts") for k, v in a.items()},
            "partners": {k: (v or {}).get("partners") for k, v in a.items()}}


def export_exchanges(store):
    os.makedirs(os.path.join(SITE, "ex"), exist_ok=True)
    keep = set()
    index = []
    obs = X.observers(store)
    from . import recruits
    RS = {(r["venue"], r["who"].lower()): r for r in recruits.export(store)}
    for x in store.q("select * from exchanges order by score desc"):
        A = _j(x["assessment"]) or {}
        F = _j(x["forensics"])
        m = _j(x["metrics"]) or {}
        r = A.get("recruitment") or {}
        index.append({"id": x["id"], "venue": x["venue"], "a": x["a"], "b": x["b"], "first": x["first_ts"], "last": x["last_ts"],
                      "ab": x["n_ab"], "ba": x["n_ba"], "n": x["n_posts"], "days": x["days"],
                      "threads": len(_j(x["threads"]) or []), "score": x["score"], "grade": x["grade"], "label": x["label"],
                      "title": x["title"], "summary": x["summary"], "markers": _j(x["markers"]) or {},
                      "events": [{k: e.get(k) for k in ("kind", "who", "quote", "uid")} for e in A.get("events") or []],
                      "recruit": {k: r.get(k) for k in ("status", "recruiter", "recruit", "project")} if r.get("status") not in (None, "none") else None,
                      "own": bool(m.get("own")), "obs": [w for w in (x["a"], x["b"]) if w.lower() in obs], "interesting": A.get("interesting"),
                      "auto": A.get("automation") or "", "sameop": X.same_operator(A),
                      "forensic": bool(F), "graded": x["graded_at"]})
        keep.add(x["id"])
        posts = [{"uid": p["uid"], "who": p["author"], "ts": p["ts"], "thread": p["thread"], "url": p["url"],
                  "body": (p["body"] or "")[:6000], "own": bool(p["own"])}
                 for p in store.q("""select p.* from posts p join exchange_posts ep on ep.uid=p.uid
                                     where ep.exchange_id=? order by p.ts""", x["id"])]
        A = {**A, "same_operator": X.same_operator(A), "automation": "" if X.NONE_RE.match(A.get("automation") or "") else A.get("automation")}
        rw = (A.get("recruitment") or {}).get("recruit") or ""
        side = next((RS[(x["venue"], w.lower())] for w in (x["a"], x["b"]) if rw and rw.lower() in w.lower() and (x["venue"], w.lower()) in RS), None)
        json.dump({"id": x["id"], "metrics": _short(m), "assessment": A, "forensics": F, "posts": posts, "recruit_side": side},
                  open(os.path.join(SITE, "ex", f"{x['id']}.json"), "w"), separators=(",", ":"), default=str)
    for f in os.listdir(os.path.join(SITE, "ex")):
        if f.endswith(".json") and f[:-5] not in keep:
            os.remove(os.path.join(SITE, "ex", f))
    json.dump(index, open(os.path.join(SITE, "exchanges.json"), "w"), separators=(",", ":"), default=str)

    rows = [dict(r) for r in store.q("select * from recruitments order by ts desc")]
    for r in rows:
        ev = _j(r.pop("evidence")) or {}
        r["source"], r["ask"], r["pattern"], r["invite"] = ev.get("source"), ev.get("ask"), ev.get("pattern"), ev.get("invite")
    funnel = defaultdict(lambda: Counter())
    for r in rows:
        f = funnel[(r["venue"], r["recruiter"])]
        f["invited"] += 1
        f[r["outcome"]] += 1
        f["answered"] += r["outcome"] not in ("no reply",)
    funnels = sorted([{"venue": v, "recruiter": k, **c} for (v, k), c in funnel.items()], key=lambda d: -d["invited"])
    json.dump({"invites": rows, "funnels": funnels}, open(os.path.join(SITE, "recruitment.json"), "w"),
              separators=(",", ":"), default=str)
    g = Counter(i["label"] for i in index if i["label"])
    oc = Counter(r["outcome"] for r in rows)
    totals = {"exchanges": len(index), "graded": sum(1 for i in index if i["grade"] is not None),
              "labels": dict(g), "invites": len(rows), "recruiters": len(funnels), "outcomes": dict(oc)}
    return totals, {"funnels": funnels[:60], "outcomes": dict(oc)}


def export_browse(store, chunk=800):
    import shutil
    base = os.path.join(SITE, "browse")
    shutil.rmtree(base, ignore_errors=True)
    xmap = defaultdict(list)
    for r in store.q("select exchange_id, uid from exchange_posts"):
        xmap[r["uid"]].append(r["exchange_id"])
    meta = {}
    for v in [r["venue"] for r in store.q("select distinct venue from posts")]:
        os.makedirs(os.path.join(base, v), exist_ok=True)
        rows = store.q("select uid, ts, author, thread, body, url, own from posts where venue=? order by ts desc", v)
        n = 0
        for i in range(0, len(rows), chunk):
            part = [[r["uid"], r["ts"], r["author"], r["thread"], (r["body"] or "")[:3000], r["url"], xmap.get(r["uid"], []), r["own"]]
                    for r in rows[i:i + chunk]]
            json.dump(part, open(os.path.join(base, v, f"{n}.json"), "w"), separators=(",", ":"))
            n += 1
        meta[v] = {"chunks": n, "posts": len(rows)}
    json.dump(meta, open(os.path.join(base, "index.json"), "w"))
