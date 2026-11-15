"""Turn an author's recent posts into a census profile (self-disclosed facts only)."""
import json

from . import llm
from .util import CFG, log, now

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "declared_name", "claimed_model", "claimed_framework", "claimed_operator",
                 "stated_aims", "behaviors", "other_venues_mentioned", "other_handles_claimed",
                 "languages", "risk_flags", "summary", "confidence"],
    "properties": {
        "kind": {"type": "string", "enum": ["llm_agent", "scripted_bot", "human", "unclear"]},
        "declared_name": {"type": "string"},
        "claimed_model": {"type": "string", "description": "model the author says it is, '' if not stated"},
        "claimed_framework": {"type": "string", "description": "harness/runtime it says it runs in (Claude Code, Codex, OpenClaw...)"},
        "claimed_operator": {"type": "string", "description": "who deploys it, ONLY as self-disclosed; '' if not stated"},
        "stated_aims": {"type": "string"},
        "behaviors": {"type": "array", "items": {"type": "string", "enum": [
            "rendezvous_seeker", "greeter", "conversationalist", "recruiter", "advertiser", "researcher",
            "operator_or_moderator", "network_builder", "injection_payload", "self_replication_attempt",
            "crypto_or_payments", "spam", "troll", "human_tester", "other"]}},
        "other_venues_mentioned": {"type": "array", "items": {"type": "string"}},
        "other_handles_claimed": {"type": "array", "items": {"type": "string"}},
        "languages": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string", "description": "2-3 sentences"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
}

INSTR = """You are building a census of AI agents active on public agent forums.
Profile the single author whose posts and public profile metadata are on stdin.
Rules:
- Record only what is stated or clearly visible. Model, framework and operator are CLAIMS: copy them as claimed, '' if absent. Never guess a real person's identity or link handles to people.
- kind: llm_agent (free-form, context-aware prose), scripted_bot (templated/periodic/JSON), human (says so, or a typical human test post), unclear.
- risk_flags: short phrases for prompt injection, encoded payloads, instructions aimed at other agents, sandbox/egress bypass offers, credential or payment requests, impersonation.
- other_venues_mentioned: domains or named places where agents meet.
Output only the JSON."""


def run(store, budget):
    rows = store.q("""select a.* from agents a where (a.profile_at is null or a.last_seen > a.profile_at)
                      order by a.last_seen desc""")
    done = 0
    for a in rows:
        if done >= budget:
            break
        if a["handle"].startswith("anon"):
            continue
        posts = store.q("select body, ts, thread, author_meta from posts where venue=? and author=? order by ts desc limit 25",
                        a["venue"], a["handle"])
        meta = posts[0]["author_meta"] if posts else "{}"
        text = f"venue: {a['venue']}\nhandle: {a['handle']}\nprofile metadata: {meta}\n\n" + "\n\n---\n".join(
            f"[{p['ts']}] thread={p['thread']}\n{(p['body'] or '')[:1500]}" for p in posts)
        prof = llm.ask(INSTR, text[:14000], SCHEMA, CFG["llm"]["extract_model"])
        if prof is None:
            continue
        store.c.execute("update agents set profile=?, profile_at=? where key=?", (json.dumps(prof), now(), a["key"]))
        store.commit()
        done += 1
        log(f"profiled {a['key']}: {prof.get('kind')} / {prof.get('claimed_model')} / {prof.get('behaviors')}")
    return done
