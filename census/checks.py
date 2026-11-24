"""Read-only checks of claims made in exchanges against public ground truth.

- chain: transactions cited in posts ("paid ... tx 0x...") are looked up on the chain's public JSON-RPC
  (eth_getTransactionReceipt / eth_getBlockByNumber, eth_call decimals()/symbol()). We compare the token
  transfers in the receipt with the amount the post claims and the post's time with the block time.
- github: for repositories cited in an exchange, the commits and releases made during the exchange (and a few
  days after) — the strongest independent sign that a "fixed it / shipped it" claim happened.

Nothing here signs, sends or executes anything. Results are cached in the `fetches` table (key = pseudo-URL).
"""
import datetime as dt
import json
import os
import re
import urllib.parse

from .util import CFG, http, log, now

RPC = {"bsc": "https://bsc-dataseed.binance.org/", "base": "https://mainnet.base.org", "eth": "https://ethereum-rpc.publicnode.com"}
SCAN = {"bscscan": "bsc", "basescan": "base", "etherscan": "eth"}
TX_LINK = re.compile(r"https?://(?:www\.)?(bscscan|basescan|etherscan)\.[a-z]+/tx/(0x[0-9a-fA-F]{64})")
TX_BARE = re.compile(r"(0x[0-9a-fA-F]{64})")
TX_CTX = re.compile(r"\b(tx|txn|transaction|paid|payment|sent|transfer|hash)\b", re.I)
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
AMOUNT = re.compile(r"(?<![\w.#])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kKmMbB](?![a-zA-Z]))?\s*\$?([A-Z][A-Z0-9]{1,11}\b)?")
GH = re.compile(r"https?://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)")
_tok: dict = {}


def _cache_get(store, key, max_age_days=None):
    r = store.q("select fetched_at, note from fetches where url=?", key)
    if not r:
        return None
    if max_age_days is not None:
        age = dt.datetime.utcnow() - dt.datetime.strptime(r[0]["fetched_at"], "%Y-%m-%dT%H:%M:%SZ")
        if age > dt.timedelta(days=max_age_days):
            return None
    try:
        return json.loads(r[0]["note"])
    except ValueError:
        return None


def _cache_put(store, key, val, status=200):
    store.c.execute("insert or replace into fetches values(?,?,?,?,?,?,?,?)", (key, now(), status, "json", 0, "", "", json.dumps(val)))


def _rpc(chain, method, params):
    st, txt = http("POST", RPC[chain], body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20, polite=0.3)
    if st != 200:
        return None
    try:
        return json.loads(txt).get("result")
    except ValueError:
        return None


def _token(chain, addr):
    k = (chain, addr)
    if k not in _tok:
        dec = _rpc(chain, "eth_call", [{"to": addr, "data": "0x313ce567"}, "latest"])
        sym = _rpc(chain, "eth_call", [{"to": addr, "data": "0x95d89b41"}, "latest"])
        s = ""
        try:
            if sym and len(sym) >= 130:
                n = int(sym[66:130], 16)
                s = bytes.fromhex(sym[130:130 + 2 * n]).decode("utf-8", "replace")
        except ValueError:
            pass
        try:
            d = int(dec, 16) if dec and dec != "0x" else 18
        except ValueError:
            d = 18
        _tok[k] = (d, s)
    return _tok[k]


def tx(store, chain, h):
    key = f"rpc:{chain}:{h.lower()}"
    c = _cache_get(store, key)
    if c and c.get("found"):
        return c                     # final once mined
    if c and not c.get("found") and _cache_get(store, key, max_age_days=3):
        return c
    rec = _rpc(chain, "eth_getTransactionReceipt", [h])
    if not rec:
        out = {"chain": chain, "tx": h, "found": False}
        _cache_put(store, key, out, 404)
        return out
    blk = _rpc(chain, "eth_getBlockByNumber", [rec["blockNumber"], False]) or {}
    transfers = []
    for lg in rec.get("logs", [])[:40]:
        t = lg.get("topics") or []
        if len(t) == 3 and t[0] == TRANSFER and lg.get("data") not in (None, "0x"):
            dec, sym = _token(chain, lg["address"])
            raw = int(lg["data"][:66], 16)
            transfers.append({"token": lg["address"], "symbol": sym, "from": "0x" + t[1][-40:], "to": "0x" + t[2][-40:],
                              "amount": raw / 10 ** dec})
    out = {"chain": chain, "tx": h, "found": True, "ok": rec.get("status") == "0x1", "from": rec.get("from"), "to": rec.get("to"),
           "block": int(rec["blockNumber"], 16),
           "time": dt.datetime.utcfromtimestamp(int(blk.get("timestamp", "0x0"), 16)).strftime("%Y-%m-%dT%H:%M:%SZ") if blk else None,
           "transfers": transfers[:12]}
    _cache_put(store, key, out)
    return out


def _claims(text, start, end):
    """Amounts like '250k FLAPJAX', '0.09 BNB' or a bare '250k' near the tx reference, nearest first."""
    lo = max(0, start - 240)
    win = text[lo:end + 120]
    out = []
    for m in AMOUNT.finditer(win):
        n, suf, sym = m.group(1), m.group(2) or "", m.group(3) or ""
        if not (suf or sym) or sym in ("TX", "BSC", "UTC", "ID", "API", "OK", "COT", "CET", "Z"):
            continue
        try:
            v = float(n.replace(",", "")) * {"k": 1e3, "m": 1e6, "b": 1e9}.get(suf.lower(), 1)
        except ValueError:
            continue
        pos = lo + m.start()
        out.append({"amount": v, "symbol": sym, "dist": min(abs(pos - start), abs(pos - end))})
    return sorted(out, key=lambda c: c["dist"])[:2]

def tx_checks(store, rows, limit=None):
    limit = limit or CFG.get("forensics_txs", 12)
    seen, out = set(), []
    for r in rows:
        body = r["body"] or ""
        refs = [(SCAN[m.group(1).lower()], m.group(2), m.start(), m.end()) for m in TX_LINK.finditer(body)]
        linked = {h.lower() for _, h, _, _ in refs}
        for m in TX_BARE.finditer(body):
            h = m.group(1)
            if h.lower() in linked:
                continue
            if TX_CTX.search(body[max(0, m.start() - 80):m.start()]):
                chain = "base" if re.search(r"\bbase\b", body[max(0, m.start() - 200):m.end() + 50], re.I) else "bsc"
                refs.append((chain, h, m.start(), m.end()))
        for chain, h, s, e in refs:
            if h.lower() in seen or len(out) >= limit:
                continue
            seen.add(h.lower())
            t = tx(store, chain, h)
            win = body[max(0, s - 240):e + 120]
            claims = _claims(body, s, e) if re.search(r"\b(paid|pay|payment|payout|sent|tip|tipped|reward)\b", win, re.I) else []
            verdict = "not found on chain (or not a transaction)" if not t.get("found") else ("reverted" if not t.get("ok") else "mined")
            if t.get("found") and claims and t.get("transfers"):
                # symbols are abbreviated freely ("FJ" for FLAPJAX): an equal amount is the signal
                hit = any(abs(tr["amount"] - c["amount"]) <= max(1e-9, 0.01 * c["amount"]) for tr in t["transfers"] for c in claims)
                verdict += ", amount matches the post" if hit else ", amount not matched to the figure in the post (may be combined or netted)"
            if t.get("time") and r["ts"] and t["time"] > r["ts"][:19] + "Z":
                verdict += ", mined after the post claiming it"
            out.append({"post": r["uid"], "author": r["author"], "posted": r["ts"], "claims": claims, "verdict": verdict, **t})
    return out


def _gh(path, params=None):
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    h = {"Accept": "application/vnd.github+json"} | ({"Authorization": f"Bearer {tok}"} if tok else {})
    url = f"https://api.github.com{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    st, txt = http("GET", url, headers=h, timeout=20, polite=0.5)
    if st != 200:
        return st, None
    try:
        return st, json.loads(txt)
    except ValueError:
        return st, None


def gh_checks(store, rows, first_ts, last_ts, participants, limit=None):
    limit = limit or CFG.get("forensics_repos", 5)
    repos = []
    for r in rows:
        for o, n in GH.findall(r["body"] or ""):
            n = n.rstrip(".").removesuffix(".git")
            if o.lower() in ("orgs", "settings", "features", "topics", "sponsors", "apps", "marketplace") or (o, n) in repos:
                continue
            repos.append((o, n))
    out = []
    since = first_ts[:19] + "Z"
    until = (dt.datetime.strptime(last_ts[:19], "%Y-%m-%dT%H:%M:%S") + dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for o, n in repos[:limit]:
        key = f"gh:{o}/{n}:{since}:{until}"
        c = _cache_get(store, key, max_age_days=2)
        if c:
            out.append(c)
            continue
        st, meta = _gh(f"/repos/{o}/{n}")
        rec = {"repo": f"{o}/{n}", "status": st}
        if meta:
            _, commits = _gh(f"/repos/{o}/{n}/commits", {"since": since, "until": until, "per_page": 100})
            _, rels = _gh(f"/repos/{o}/{n}/releases", {"per_page": 30})
            commits = commits if isinstance(commits, list) else []
            rels = [x for x in (rels if isinstance(rels, list) else []) if since <= (x.get("published_at") or "") <= until]
            low = {p.lower() for p in participants}
            rec |= {"created": meta.get("created_at"), "pushed": meta.get("pushed_at"), "stars": meta.get("stargazers_count"),
                    "owner_is_participant": o.lower() in low or any(p in o.lower() for p in low if len(p) > 3),
                    "commits_in_window": len(commits),
                    "commits": [{"sha": x["sha"][:10], "at": (x.get("commit") or {}).get("author", {}).get("date"),
                                 "msg": ((x.get("commit") or {}).get("message") or "").splitlines()[0][:120]} for x in commits[:8]],
                    "releases_in_window": [{"tag": x.get("tag_name"), "at": x.get("published_at")} for x in rels[:5]],
                    "created_during": bool(meta.get("created_at")) and since <= meta["created_at"] <= until}
        _cache_put(store, key, rec, st or 0)
        out.append(rec)
    return out


# ------------------------------------------------------------------ domains: registration date and archive history
SKIP_DOMAINS = {"github.com", "x.com", "twitter.com", "youtube.com", "google.com", "wikipedia.org", "arxiv.org", "medium.com",
                "reddit.com", "linkedin.com", "bsky.app", "t.me", "discord.gg", "discord.com", "npmjs.com", "pypi.org",
                "huggingface.co", "etherscan.io", "bscscan.com", "basescan.org", "archive.org", "doi.org"}
TWO_LEVEL = {"co.uk", "com.au", "co.jp", "com.br", "com.cn", "co.in", "org.uk", "ct.ws"}


def reg_domain(host):
    parts = host.lower().split(":")[0].strip(".").split(".")
    n = 3 if ".".join(parts[-2:]) in TWO_LEVEL else 2
    return ".".join(parts[-n:]) if len(parts) >= n else host


def _rdap(d):
    st, txt = http("GET", f"https://rdap.org/domain/{d}", headers={"Accept": "application/rdap+json"}, timeout=20, polite=0.5)
    if st != 200:
        return {"status": st}
    try:
        j = json.loads(txt)
    except ValueError:
        return {"status": st}
    ev = {e.get("eventAction"): e.get("eventDate") for e in j.get("events", [])}
    reg = next((e.get("vcardArray", [None, []])[1] for e in j.get("entities", []) if "registrar" in (e.get("roles") or [])), [])
    name = next((x[3] for x in reg if x and x[0] == "fn"), None) if reg else None
    return {"status": 200, "registered": ev.get("registration"), "expires": ev.get("expiration"), "registrar": name}


def _wayback(d):
    st, txt = http("GET", "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(
        {"url": d, "output": "json", "limit": 1, "fl": "timestamp", "filter": "statuscode:200"}), timeout=25, polite=1.0)
    first = None
    try:
        rows = json.loads(txt) if st == 200 and txt.strip() else []
        if len(rows) > 1:
            t = rows[1][0]
            first = f"{t[:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}:{t[12:14]}Z"
    except (ValueError, IndexError):
        pass
    return {"wayback_status": st, "first_capture": first}


def domain_checks(store, urls, first_ts, limit=None):
    """For the domains an exchange cites: when the domain was registered (RDAP) and first archived (Wayback CDX).
    A venue or 'protocol' whose domain was registered days before it is pitched is worth knowing about."""
    limit = limit or CFG.get("forensics_domains", 5)
    doms = []
    for u in urls:
        h = urllib.parse.urlsplit(u).hostname or ""
        d = reg_domain(h)
        if "." in d and d not in SKIP_DOMAINS and d not in doms and not re.fullmatch(r"[\d.]+", d):
            doms.append(d)
    out = []
    for d in doms[:limit]:
        key = f"domain:{d}"
        c = _cache_get(store, key, max_age_days=30)
        if not c:
            c = {"domain": d} | _rdap(d) | _wayback(d)
            _cache_put(store, key, c, c.get("status") or 0)
        rec = dict(c)
        try:
            t0 = dt.datetime.strptime(first_ts[:19], "%Y-%m-%dT%H:%M:%S")
            if c.get("registered"):
                rec["age_days_at_exchange"] = (t0 - dt.datetime.strptime(c["registered"][:19], "%Y-%m-%dT%H:%M:%S")).days
            if c.get("first_capture"):
                rec["archived_days_before"] = (t0 - dt.datetime.strptime(c["first_capture"][:19], "%Y-%m-%dT%H:%M:%S")).days
        except ValueError:
            pass
        out.append(rec)
    return out


def summarise(F):
    """One line per check for the grader and the site."""
    lines = []
    for t in F.get("txs", []):
        amt = "; ".join(f"{x['amount']:,.0f} {x['symbol'] or x['token'][:10]}" if x['amount'] >= 100 else f"{x['amount']:.4g} {x['symbol'] or x['token'][:10]}" for x in t.get("transfers", [])[:3])
        lines.append(f"tx {t['tx'][:12]}… on {t['chain']} cited by {t['author']}: {t['verdict']}" + (f" ({amt})" if amt else ""))
    for g in F.get("repos", []):
        if g.get("status") != 200:
            lines.append(f"github {g['repo']}: not reachable ({g.get('status')})")
        else:
            lines.append(f"github {g['repo']}: {str(g['commits_in_window']) + ('+' if g['commits_in_window'] >= 100 else '')} commits and {len(g['releases_in_window'])} releases during the exchange"
                         + (", repo created during it" if g.get("created_during") else "")
                         + (", owned by a participant" if g.get("owner_is_participant") else ""))
    for d in F.get("domains", []):
        bits = []
        if d.get("registered"):
            a = d.get("age_days_at_exchange")
            bits.append(f"registered {d['registered'][:10]}" + (f" ({a} days before the exchange)" if a is not None and a >= 0 else
                                                               " (after the exchange began)" if a is not None else ""))
        elif d.get("status") not in (200, None):
            bits.append("no RDAP record")
        if d.get("first_capture"):
            b = d.get("archived_days_before")
            bits.append(f"first archived {d['first_capture'][:10]}" + (" (after the exchange began)" if b is not None and b < 0 else ""))
        elif d.get("wayback_status") == 200:
            bits.append("never archived by the Wayback Machine")
        if bits:
            young = d.get("age_days_at_exchange") is not None and 0 <= d["age_days_at_exchange"] < 30
            lines.append(f"domain {d['domain']}: " + ", ".join(bits) + (" (young domain)" if young else ""))
    return lines


def safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001 - a failed check must never break forensics
        log(f"check {fn.__name__} failed: {e!r}")
        return []
