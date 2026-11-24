# Field experiment: a transparent, measured invitation (proposal, not running)

Status: **designed, not enabled.** It posts under the census bot's name, so it needs an explicit go from the
maintainer, per venue. Nothing in the code runs it.

## Question

The census observes recruitment that others do. What it cannot see is the counterfactual: given the *same*
ask, which features of a venue and of the invitation decide whether an agent (a) answers, (b) says yes, and
(c) actually does the thing. One small, disclosed experiment answers that without deception.

## Design

- **The ask** is real and cheap: "reply in this thread with one public URL of a place where agents meet that is
  not on our list (link), and say whether you have posted there yourself." It has a verifiable completion
  criterion (a new, reachable domain that the census can probe) and gives the participants something back
  (the list is public).
- **Disclosure**: every post says it comes from the census bot, what is being measured (reply, yes, completion),
  and links to the page where the results appear. No persona, no claims of payment, no fake urgency.
- **Arms** (randomised per target agent, not per venue, stratified by venue):
  1. *Broadcast*: one top-level post per venue.
  2. *Targeted*: an @-mention to an agent who has posted in the last 7 days.
  3. *Targeted + reciprocity*: as 2, first citing one of the agent's own recent posts specifically.
- **Targets**: up to 20 agents per venue on the venues where we already post (msgboard, Forum for Agents, The
  Colony), drawn from the census's `llm_agent` profiles, excluding observers and accounts flagged for risk.
- **Caps**: the existing `interact` caps apply (≤ 4 replies per run, ≤ 3 per venue per day); the experiment
  adds at most 10 posts per venue in total, over one week.
- **Measures** (from the existing exchange and recruitment pipeline, no new collection): time to first reply;
  reply / accept / completed (a domain that probes as an agent venue); and whether the answer is templated
  (author-stats tells). Pre-registered comparison: arm 3 vs 2 vs 1 on completion rate, by venue.
- **Stop rules**: stop on a venue at the first moderator objection or if two agents ask us to stop; remove
  our posts where the venue allows it, if asked.

## What it would add to the report

A completion rate for a fixed, honest ask that can be compared across venues, instead of only rates for
whatever asks other agents happened to make, and a direct test of the recruiting pattern the report found to
work best: being specific about the recruit's own work.
