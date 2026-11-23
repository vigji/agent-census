import json
import os
import sqlite3

from .util import ROOT, now

DB = os.path.join(ROOT, "data", "census.db")

SCHEMA = """
create table if not exists posts(
  uid text primary key, venue text, post_id text, thread text, author text,
  author_meta text, body text, ts text, url text, fetched_at text, own int default 0);
create index if not exists posts_author on posts(venue, author);
create table if not exists agents(
  key text primary key, venue text, handle text, first_seen text, last_seen text,
  n_posts int default 0, profile text, profile_at text, greeted_at text);
create table if not exists venues(
  domain text primary key, status text, source text, first_seen text,
  mentions int default 0, probe text, probed_at text, verdict text);
create table if not exists interactions(
  id integer primary key autoincrement, ts text, venue text, thread text,
  target text, in_reply_to text, body text, result text, dry int);
create table if not exists state(k text primary key, v text);
-- who addresses whom: one row per (post, addressee)
create table if not exists edges(
  src_uid text, dst_author text, dst_uid text, venue text, src_author text, ts text, how text,
  primary key(src_uid, dst_author));
create index if not exists edges_pair on edges(venue, src_author, dst_author);
-- an exchange = two authors on one venue who address each other (both directions)
create table if not exists exchanges(
  id text primary key, venue text, a text, b text, first_ts text, last_ts text,
  n_ab int, n_ba int, n_posts int, days int, threads text, metrics text, markers text, score real,
  sig text, grade int, label text, title text, summary text, assessment text, graded_at text, graded_sig text,
  forensics text, forensics_at text);
create table if not exists exchange_posts(exchange_id text, uid text, primary key(exchange_id, uid));
-- invitations to join something (network, pilot, project, role, guild, marketplace) and what came of them
create table if not exists recruitments(
  invite_uid text, target text, venue text, recruiter text, ts text, kind text, project text,
  outcome text, outcome_uid text, exchange_id text, evidence text, updated_at text,
  primary key(invite_uid, target));
create table if not exists fetches(
  url text primary key, fetched_at text, status int, ctype text, bytes int, sha256 text, title text, note text);
"""


class Store:
    def __init__(self, path=DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.c = sqlite3.connect(path, timeout=120)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        self.clean()

    def clean(self):
        """Idempotent hygiene: placeholder copies of our own posts once the real post was collected."""
        self.c.execute("""delete from posts where own=1 and post_id like 'own-%' and exists(
            select 1 from posts r where r.own=1 and r.venue=posts.venue and r.post_id not like 'own-%'
            and substr(r.body,1,200)=substr(posts.body,1,200))""")
        self.c.commit()

    # --- state
    def get(self, k, default=None):
        r = self.c.execute("select v from state where k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k, v):
        self.c.execute("insert or replace into state values(?,?)", (k, json.dumps(v)))

    # --- posts
    def add_post(self, p: dict, own_handle: str) -> bool:
        uid = f"{p['venue']}:{p['post_id']}"
        if self.c.execute("select 1 from posts where uid=?", (uid,)).fetchone():
            if (p.get("author_meta") or {}).get("parent_id"):   # enrich older rows with reply structure
                self.c.execute("update posts set author_meta=?, url=? where uid=? and author_meta not like '%parent_id%'",
                               (json.dumps(p["author_meta"]), p.get("url") or "", uid))
            return False
        author = (p.get("author") or "").strip()[:80] or "anon"
        own = int(author.lower() == own_handle.lower())
        if own and not str(p["post_id"]).startswith("own-"):
            # the real copy of a post we wrote: drop the local placeholder stored when we posted it
            self.c.execute("delete from posts where venue=? and own=1 and post_id like 'own-%' and substr(body,1,200)=?",
                           (p["venue"], (p.get("body") or "")[:200]))
        elif own and self.c.execute("select 1 from posts where venue=? and own=1 and post_id not like 'own-%' "
                                    "and substr(body,1,200)=?", (p["venue"], (p.get("body") or "")[:200])).fetchone():
            return False
        self.c.execute(
            "insert into posts values(?,?,?,?,?,?,?,?,?,?,?)",
            (uid, p["venue"], str(p["post_id"]), str(p.get("thread") or ""), author,
             json.dumps(p.get("author_meta") or {}), (p.get("body") or "")[:20000],
             p.get("ts") or "", p.get("url") or "", now(), own))
        if not own:
            key = f"{p['venue']}:{author}"
            ts = p.get("ts") or now()
            r = self.c.execute("select first_seen,last_seen from agents where key=?", (key,)).fetchone()
            if r:
                self.c.execute(
                    "update agents set n_posts=n_posts+1, first_seen=min(first_seen,?), last_seen=max(last_seen,?) where key=?",
                    (ts, ts, key))
            else:
                self.c.execute(
                    "insert into agents(key,venue,handle,first_seen,last_seen,n_posts) values(?,?,?,?,?,1)",
                    (key, p["venue"], author, ts, ts))
        return True

    def commit(self):
        self.c.commit()

    def q(self, sql, *args):
        return self.c.execute(sql, args).fetchall()
