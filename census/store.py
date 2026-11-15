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
"""


class Store:
    def __init__(self, path=DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)

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
            return False
        author = (p.get("author") or "").strip()[:80] or "anon"
        own = int(author.lower() == own_handle.lower())
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
