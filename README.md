[swerv-submission-note.md](https://github.com/user-attachments/files/31469066/swerv-submission-note.md)
# nordgreen-compe-monitor# Swerv Digital — Second-Round Task Submission
**Marketing AI Engineer (GTM Engineer)**
Knight Charles Lacsina

---

## What I built

A competitor-monitoring system that watches Nordgreen (a Danish DTC watch brand) on a schedule, detects real catalog changes, uses Claude to judge whether each change is worth a human's attention, and delivers only the material ones to Slack — everything else is logged silently.

Built in n8n, backed by Claude for the one judgment call that can't be hardcoded, with run history logged to a Google Sheet as proof of life.

---

## 1. What I chose to monitor, and why — and what I tried that didn't work

I picked **Nordgreen**, a Danish minimalist watch brand, for a few concrete reasons:
- Danish/Nordic relevance, matching the kind of brand Swerv's own clients would realistically want watched
- Confirmed to run on Shopify, which exposes a free, structured, unauthenticated `/products.json` endpoint — no scraping fragility, no login walls
- A large enough live catalog (~250 products) with real, ongoing pricing and inventory activity, so the system would have genuine signal to catch over the observation window, not a dead storefront

**What I tried and dropped:** I initially considered pairing product monitoring with Meta Ad Library data to also catch messaging/creative changes. I deprioritized this — it needed ad account API access I didn't have time to set up cleanly within the window, and mixing two source types would have doubled the surface area for bugs before I'd even proven the core loop worked. I treated it as a clear v2 extension rather than something to half-build (see section 6).

---

## 2. Where I used AI, and why there

**Claude is used exactly once per run, in one batched call** — after all changes for that run have already been detected by deterministic code. It is not used for fetching, hashing, diffing, or formatting; those are all plain logic with zero token cost.

**Why there specifically:** the one thing code genuinely can't do reliably is decide whether a *specific* change matters in context — a $2 price bump might be a meaningful reprice or just currency-conversion noise, and that judgment depends on the surrounding numbers, not a fixed rule. That's the one link in the chain where a model earns its cost.

I deliberately **did not** reach for Claude Skills or an agent framework for this. I considered both, but this task is a single bounded judgment call, not multi-step reasoning or a document-generation workflow — reaching for either would have added infrastructure and unpredictability for no benefit, which runs against the efficiency principle Swerv described to me directly (batch, don't loop; use AI only where code genuinely can't do the job). Keeping the AI surface area to one tightly-scoped prompt, with everything else in deterministic code around it, was the deliberate design choice.

---

## 3. What happens when the model gets it wrong — because it will

Two real failures came up during the actual build, not hypotheticals:

**Truncated responses on the first run.** The very first run treats every product as "new" (nothing to compare against yet), which produced a 250-item batch in one Claude call. That batch exceeded the token budget I'd allotted, and the response cut off mid-JSON — a parsing failure, not a logic failure. The real fix wasn't a bigger token limit; it was recognizing that a first-run baseline isn't a genuine signal at all and should never reach Claude in the first place. I added a baseline-detection guard: on the first run, the system silently records the full catalog as its starting state and reports nothing, exactly as it should — "everything is new" is not information, it's the system waking up.

**Silent misparsing from an unexpected response shape.** Claude's response includes an internal reasoning block ahead of the actual answer block. My first parser assumed the answer was always in a fixed position and silently pulled the wrong block, returning `undefined`. I fixed this by having the parser search for the block by its actual type rather than assuming its position — a small but important lesson in not trusting response shape assumptions.

**The general fail-safe:** whenever Claude's response can't be parsed or a specific item gets no verdict at all, the system defaults to `worth_reporting: true` rather than silently dropping it. It would rather over-alert with a flagged "needs manual review" note than let a real change disappear silently.

---

## 4. How I decide something is worth reporting versus noise

A two-stage filter, cheapest checks first:

1. **Hash comparison (zero cost).** Every tracked product is normalized and hashed; if the hash hasn't changed, nothing happened, full stop — no further processing at all.
2. **Deterministic pre-filtering (zero cost).** Sub-$1 price fluctuations are filtered out in code before anything reaches Claude, since that's almost always currency-rounding noise, not a real reprice.
3. **Claude judgment (the only step that costs tokens).** Only genuinely ambiguous, already-filtered diffs reach the model, batched into a single call per run. Diffs are explicitly typed before they reach Claude — `new_product`, `price_change`, `stock_out`, `restock`, `sale_started`, `sale_ended` — giving it structured signal to reason over instead of a generic "something changed."

This ordering matters: the expensive, judgment-based layer only ever sees what the cheap layers couldn't already resolve.

---

## 5. What breaks first if this ran across 20 brands instead of 1

- **Shared state becomes a single point of failure.** Right now, all tracked state lives in one place. At 20 brands, that needs to be partitioned per brand — a corruption or reset in one brand's state shouldn't be able to touch another's.
- **Rate limits, on both ends.** Free-tier limits on both the source sites and Claude's API get hit fast at 20x the polling volume. A linear, sequential per-brand schedule doesn't scale — this would need a queue/worker pattern so brands are processed independently and in parallel, not one after another in a single run.
- **Silent-failure risk multiplies.** With one brand, I can watch the Slack channel and sheet daily. At 20, a single brand's source quietly breaking (a URL change, a new bot-block) could go unnoticed for days unless the degraded-run alerting is loud and per-brand, not just global.

---

## 6. What I wanted to build but couldn't — and how I'd do it with the right tool

- **Ad creative and messaging monitoring**, via Meta Ad Library, to catch positioning and campaign shifts, not just catalog changes. This needs ad account API access and likely a vision model to diff creative assets meaningfully — both out of scope for what I could responsibly stand up on a free tier in this window.
- **Collection-level tracking** (new collections appearing, being renamed, or reprioritized) as a proxy for campaign launches — a straightforward extension of the same pattern I already built for products, deprioritized purely for time, not difficulty.
- **Sale/promotion tracking via `compare_at_price`** — I identified this as a high-value, low-effort addition partway through (it's already in the Shopify feed I'm pulling) and began wiring it in, but held off finishing it to keep the core five requirements rock-solid first rather than risk destabilizing a working system this close to submission.

---

## A note on the build process itself

Most of what's above came from debugging a live system, not from planning on paper. Along the way I hit and fixed: a GET-vs-POST bug on two separate delivery nodes, a JSON body double-encoding issue, an object-reference bug where a state reset silently did nothing because a variable had already captured the old reference before the reset ran, and n8n's static data not persisting the way I expected during manual test executions versus real scheduled ones. None of these were visible until the system was actually running against real data on a real schedule — which is, I think, exactly the point of building the thing rather than just planning it.

---

## Proof of runs

- Schedule ran automatically at both an hourly test cadence (for fast debugging feedback) and the production 6-hour cadence used for the actual observation window
- Every run — quiet, material, or degraded — is logged as a timestamped row in the attached Google Sheet
- A deliberate failure test (pointing the fetch step at a broken URL) confirmed the degraded-alert path fires correctly and logs distinctly from a normal quiet run
- Slack channel `#nordgreen-prod-monitoring` contains the full history of real alerts sent, including reasoning attached to each one
