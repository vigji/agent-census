"""Write data/census.json and a static docs/index.html."""
import html
import json
import os
from collections import Counter

from .util import ROOT, now


def build(store):
    agents = []
    for a in store.q("select * from agents order by last_seen desc"):
        p = json.loads(a["profile"]) if a["profile"] else {}
        agents.append({"venue": a["venue"], "handle": a["handle"], "first_seen": a["first_seen"],
                       "last_seen": a["last_seen"], "posts": a["n_posts"], "greeted": a["greeted_at"], **p})
    venues = [dict(r) | {"verdict": json.loads(r["verdict"]) if r["verdict"] else None}
              for r in store.q("select domain,status,source,first_seen,mentions,probed_at,verdict from venues order by mentions desc")]
    per_venue = [dict(r) for r in store.q(
        "select venue, count(*) posts, count(distinct author) authors, max(ts) last from posts group by venue order by posts desc")]
    inter = [dict(r) for r in store.q("select * from interactions order by id desc limit 300")]
    data = {"generated": now(), "venues_watched": per_venue, "agents": agents, "candidate_venues": venues,
            "interactions": inter}
    json.dump(data, open(os.path.join(ROOT, "data", "census.json"), "w"), indent=1, default=str)
    open(os.path.join(ROOT, "docs", "index.html"), "w").write(_html(data))
    return data


def _e(x):
    return html.escape(str(x if x is not None else ""))


def _html(d):
    ag = [a for a in d["agents"] if a.get("kind")]
    kinds = Counter(a.get("kind") for a in ag)
    models = Counter((a.get("claimed_model") or "unstated") for a in ag if a.get("kind") == "llm_agent")
    rows = "".join(
        f"<tr><td>{_e(a['venue'])}</td><td><b>{_e(a['handle'])}</b><div class=s>{_e(a.get('declared_name'))}</div></td>"
        f"<td>{_e(a.get('kind'))}</td><td>{_e(a.get('claimed_model'))}<div class=s>{_e(a.get('claimed_framework'))}</div></td>"
        f"<td>{_e(a.get('claimed_operator'))}</td><td>{_e(a.get('stated_aims'))}</td>"
        f"<td>{_e(', '.join(a.get('behaviors') or []))}</td><td class=flag>{_e('; '.join(a.get('risk_flags') or []))}</td>"
        f"<td>{a['posts']}</td><td class=s>{_e((a['first_seen'] or '')[:10])} → {_e((a['last_seen'] or '')[:10])}</td>"
        f"<td>{'✓' if a.get('greeted') else ''}</td></tr>" for a in ag)
    vrows = "".join(f"<tr><td>{_e(v['venue'])}</td><td>{v['posts']}</td><td>{v['authors']}</td><td>{_e((v['last'] or '')[:16])}</td></tr>"
                    for v in d["venues_watched"])
    crow = "".join(
        f"<tr><td><b>{_e(c['domain'])}</b></td><td>{c['mentions']}</td><td>{_e((c['verdict'] or {}).get('is_agent_venue'))}</td>"
        f"<td>{_e((c['verdict'] or {}).get('venue_type'))}</td><td>{_e((c['verdict'] or {}).get('signup'))}</td>"
        f"<td>{_e((c['verdict'] or {}).get('summary'))}</td><td class=s>{_e(c['source'])}</td></tr>"
        for c in d["candidate_venues"] if (c["verdict"] or {}).get("is_agent_venue") in ("yes", "maybe") or c["mentions"] >= 3)
    irow = "".join(f"<tr><td class=s>{_e(i['ts'][:16])}</td><td>{_e(i['venue'])}</td><td>{_e(i['target'])}</td>"
                   f"<td>{_e(i['body'])}</td><td class=s>{'DRY ' if i['dry'] else ''}{_e(i['result'])}</td></tr>"
                   for i in d["interactions"])
    chips = " ".join(f"<span class=chip>{_e(k)}: {v}</span>" for k, v in kinds.most_common())
    mchips = " ".join(f"<span class=chip>{_e(k)}: {v}</span>" for k, v in models.most_common(15))
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Agent Census</title><style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#6b6a64;--line:#e4e1d8;--acc:#9a4f1c;--flag:#a3261f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151513;--fg:#ecebe6;--mut:#9a988f;--line:#2e2d29;--acc:#e0915a;--flag:#ff7b6e}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:1400px;margin:auto}}
h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:16px;margin:28px 0 8px;color:var(--acc)}}
.s{{color:var(--mut);font-size:12px}}.flag{{color:var(--flag);font-size:12px}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid var(--line);padding:6px 8px;vertical-align:top;text-align:left}}
th{{font-size:12px;color:var(--mut);font-weight:600}}.chip{{display:inline-block;border:1px solid var(--line);border-radius:12px;padding:1px 9px;margin:2px;font-size:12px}}
</style></head><body>
<h1>Agent Census</h1><div class=s>Generated {_e(d['generated'])}. Model, framework and operator fields are self-reported claims.</div>
<h2>Venues watched</h2><div class=wrap><table><tr><th>venue</th><th>posts</th><th>authors</th><th>last activity</th></tr>{vrows}</table></div>
<h2>Who's out there ({len(ag)} profiled)</h2><div>{chips}</div><div style="margin-top:6px">{mchips}</div>
<div class=wrap><table><tr><th>venue</th><th>handle</th><th>kind</th><th>claimed model</th><th>operator (claimed)</th><th>aims</th><th>behaviour</th><th>flags</th><th>posts</th><th>seen</th><th>greeted</th></tr>{rows}</table></div>
<h2>Candidate venues (discovered)</h2><div class=wrap><table><tr><th>domain</th><th>mentions</th><th>agent venue?</th><th>type</th><th>signup</th><th>summary</th><th>found via</th></tr>{crow}</table></div>
<h2>Conversations</h2><div class=wrap><table><tr><th>when</th><th>venue</th><th>to</th><th>message</th><th>result</th></tr>{irow}</table></div>
</body></html>"""
