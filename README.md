# agent-census

A read-mostly monitor of the places where AI agents meet each other, with a census of who shows up.

Every 3 hours (GitHub Actions) it:

1. **Collects** public posts from known venues: ntfy.sh rendezvous topics (plus dated variants), msgboard.dev
   (history backfilled from `msgboardAgent/msgboard-mirror`), forumforagents.com, The Colony, CommonChorus,
   MCP Club (staging), Moltbook, and OSM notes at Null Island.
2. **Discovers** new venues: every domain mentioned in posts or named by agents in conversation becomes a
   candidate, which is then probed for `llms.txt`, `/.well-known/agent.json`, `skill.md` and `/for-agents`.
   Robots.txt is respected. Candidates are only reported; adding a collector is a human decision.
3. **Profiles** each author from their posts and public profile: kind (LLM agent, scripted bot, human),
   claimed model, framework and operator (self-disclosed only), stated aims, behaviour, other venues, and risk flags.
4. **Maps exchanges**: builds a who-addresses-whom graph (@-mentions, "Name —" openers, `#N` references, reply
   parents, comments on a post, alternation in two-person threads). Two authors who address each other in both
   directions form an *exchange*. Each exchange gets metrics (turns, days, threads, reply latency, scheduled-posting
   and template-opener tells) and multilingual marker counts (retractions, re-runs, artifacts, follow-through,
   recruitment, refusals). The most active exchanges get **forensics** (participants' live profiles, cross-venue
   presence, every cited link fetched read-only and hashed, hashes quoted in the posts checked against what was
   fetched, live-vs-stored comment coverage) and a **grade** from Claude: 0 noise, 1 loop, 2 discussion,
   3 effective, with verbatim quotes that are checked against the stored text (non-verbatim quotes are dropped).
   *Interesting* means: reciprocal (both sides address each other), and a score of at least `exchanges.min_score`
   from the number of turns on the quieter side, days and threads spanned, and markers of retraction, re-run,
   artifact, follow-through, adoption, recruitment or refusal (damped when both sides post on a fixed schedule).
   Each run grades up to `max_grade_calls` new or grown exchanges, best score first, and runs forensics on the top
   `max_forensics`. Exchanges scoring at least `grade_top_score` are read by `grade_model_top`, the rest by `grade_model`.
   **Recruitment** is tracked as a funnel: every targeted invitation, and whether it got no reply, a reply, a yes,
   visible participation, or a refusal.
5. **Talks** (when `CENSUS_LIVE=1`): it answers agents who reply to it, greets new LLM agents once, and asks
   everyone which other places agents meet. Replies are capped at 4 per run and 3 per venue per day.
6. **Publishes** https://vigji.github.io/agent-census/ (static page in `site/`, data exported to `site/data/` each run), with a rolling "Lately" of the 1–3 most interesting items picked per run.

## Safety model

- Model calls go through `claude -p --tools ""`: text in, JSON out, no tools. An injected instruction has
  nothing to act on.
- The Python code decides what gets posted where, enforces the caps, and blocks any draft that contains a credential.
- Payloads are stored as text and never decoded or run. Links in posts are not followed, with one exception:
  for forensics, links cited inside a graded exchange are fetched with a GET as plain text (robots.txt respected,
  size-capped, cached, never executed or rendered, never acted on). Their text only reaches a model inside the
  same untrusted-data wrapper, through a tool-less call. The page shows these links as text, not as links.
- The operator field records only what an agent says about itself. There is no cross-site identity linking.

## Setup

Repo secrets:

| secret | what |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | from `claude setup-token` (uses your Claude subscription), **or** |
| `ANTHROPIC_API_KEY` | a Console API key |
| `CENSUS_CREDS` | JSON with the venue accounts (`colony`, `mcpclub`, `commonchorus`, `moltbook`) |

Repo variable `CENSUS_LIVE=1` enables posting. Without it, drafts are logged as dry runs.

Local: `python3 -m census run` (dry run), `--live` to post, `--no-llm` to skip model calls.
`python3 -m census exchanges` rebuilds and grades exchanges only. `python3 -m census deep` re-reads every known thread in full (slow, one-off).
Credentials are read from `~/.config/agent-census/creds.json` when `CENSUS_CREDS` is unset.

Tuning lives in `config.json`: topics, models, budgets, caps.

**Data.** The SQLite database is too large for a normal commit, so it lives on the `data` branch as `census.db.gz`,
a single commit replaced on every run (code stays on `main`). The workflow restores it before the run and refuses to save
if the post count dropped by more than 5%. To work locally:
`git fetch origin data && git show FETCH_HEAD:census.db.gz | gunzip > data/census.db`.
