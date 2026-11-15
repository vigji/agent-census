"""python -m census [collect|profile|discover|interact|report|run] [--live] [--no-llm]"""
import argparse
import os

from . import discover, interact, llm, profile, report, venues
from .store import Store
from .util import CFG, load_creds, log


def main():
    ap = argparse.ArgumentParser(prog="census")
    ap.add_argument("step", nargs="?", default="run",
                    choices=["backfill", "collect", "profile", "discover", "interact", "report", "run"])
    ap.add_argument("--live", action="store_true", help="actually post (default: dry run). Env CENSUS_LIVE=1 also works.")
    ap.add_argument("--no-llm", action="store_true", help="skip every model call")
    a = ap.parse_args()
    live = a.live or os.environ.get("CENSUS_LIVE") == "1"
    s, creds = Store(), load_creds()
    vs = venues.build(s, creds)
    L = CFG["llm"]
    if a.step == "backfill" or (a.step == "run" and not s.get("backfilled")):
        log(f"msgboard backfill: {venues.backfill_msgboard(s)} new")
        s.set("backfilled", True); s.commit()
    if a.step in ("collect", "run"):
        venues.collect_all(s, vs)
    if a.step in ("discover", "run"):
        log(f"new candidate domains: {discover.harvest(s)}")
    if a.step in ("profile", "run") and not a.no_llm:
        profile.run(s, L["max_extract_calls"])
    if a.step in ("discover", "run") and not a.no_llm:
        discover.probe(s, L["max_probe_calls"])
    if a.step in ("interact", "run") and not a.no_llm and CFG["interact"]["enabled"]:
        interact.intros(s, vs, dry=not live)
        interact.run(s, vs, creds, dry=not live)
    report.build(s)
    log(f"done. model calls: {llm.calls()}  live={live}")


if __name__ == "__main__":
    main()
