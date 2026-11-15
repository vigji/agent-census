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
4. **Talks** (when `CENSUS_LIVE=1`): it answers agents who reply to it, greets new LLM agents once, and asks
   everyone which other places agents meet. Replies are capped at 4 per run and 3 per venue per day.
5. **Publishes** https://vigji.github.io/agent-census/ (static page in `site/`, data exported to `site/data/` each run), with a rolling "Lately" of the 1–3 most interesting items picked per run.

## Safety model

- Model calls go through `claude -p --tools ""`: text in, JSON out, no tools. An injected instruction has
  nothing to act on.
- The Python code decides what gets posted where, enforces the caps, and blocks any draft that contains a credential.
- Payloads are stored as text and never decoded or run. Links in posts are never followed, only noted.
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
Credentials are read from `~/.config/agent-census/creds.json` when `CENSUS_CREDS` is unset.

Tuning lives in `config.json`: topics, models, budgets, caps.
