"""Export the data behind the public page (site/data/) and a summary copy in data/census.json.

summary.json        totals, venues, agents (profiles, no posts), discovered venues, conversations, highlights
posts/<venue>.json  {author: [[ts, thread, body, url], ...]}, loaded by the page only when needed
"""
import json
import os
from collections import Counter, defaultdict

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

    by_venue = defaultdict(lambda: defaultdict(list))
    for r in store.q("select venue, author, ts, thread, body, url from posts where own=0 order by ts desc"):
        lst = by_venue[r["venue"]][r["author"]]
        if len(lst) < 60:
            lst.append([r["ts"], r["thread"], (r["body"] or "")[:2500], r["url"]])
    for v, d in by_venue.items():
        json.dump(d, open(os.path.join(SITE, "posts", f"{v}.json"), "w"), separators=(",", ":"))
    json.dump(summary, open(os.path.join(SITE, "summary.json"), "w"), separators=(",", ":"), default=str)
    json.dump(summary, open(os.path.join(ROOT, "data", "census.json"), "w"), indent=1, default=str)
    return summary
