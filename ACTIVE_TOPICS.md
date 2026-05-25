# ACTIVE_TOPICS.md

**Universal topic index.** Read this FIRST after any compaction, model switch, or `/new`.
This covers ALL active projects/topics — not just alpha scout.

---

## Active Topics

- **Alpha Scout / X-Twitter Sniper** → `memory/topics/ALPHASCOUTMEMORY.md`
  - status: **active** — cron 30m running
  - updated: 2026-04-26T07:35Z
  - summary: Cap raised 500→1000, hard timeout added, crypto passthrough tightened, xreach circuit breaker improved. Cron ON `*/30 * * * *`. Pending: reject Notion schema fix, new auth tokens, precision validation.

- **China Alpha / CT Sniper** → `memory/topics/CHINAALPHAMEMORY.md`
  - status: active
  - updated: 2026-04-19T16:08Z
  - summary: China seeds routed to separate Notion DB. Stable.

- **AWP / Predict WorkNet** → `memory/topics/AWPMEMORY.md`
  - status: active (low priority)
  - updated: 2026-04-17T15:40Z
  - summary: Wallet/reward/export continuity. Production validation pending.

---

## Archived Topics

_(none yet — move topics here when done/inactive 2+ weeks)_

---

## How to use this file

1. After compaction/reset → read this file first
2. Match user's question to a topic above
3. Read that topic's `*MEMORY.md` file (especially the `⚡ COMPACT-SAFE STATUS` section)
4. Read today's + yesterday's daily memory
5. Then act

**Adding new topics:** When a new project/topic becomes recurring (3+ mentions), create `memory/topics/[NAME]MEMORY.md` with the standard format and add an entry here.
- `OPENCLAW-UPDATE-INCIDENT-MEMORY.md` — active incident/runbook for OpenClaw 2026.4.27 update/restart stuck/rate-limit/context/duplicate cascade; use before any future OpenClaw update/downgrade.
- `TWITTERSNIPPERMEMORY.md` — active; X/Twitter alpha + NFT discovery bots, Notion pipelines, GitHub backup.
