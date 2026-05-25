# AWPMEMORY.md

- Title: AWP / Predict WorkNet / wallet + rewards
- Kind: project
- Created: 2026-04-17T15:40:00Z
- Status: active

## ⚡ COMPACT-SAFE STATUS (read this first after any reset/compaction)
**Last updated:** 2026-05-25T18:45Z

### Validator (active)
- **Process:** Running (PID 3032, started 2026-05-25T18:00 UTC, session `validator-1779732006`)
- **Status:** eligible=true, in_ready_pool=true, ws_connected=true, phase=waiting_for_task
- **Stats:** tasks_received=17, tasks_evaluated=13, tasks_match=11, mismatch=0, errors=2, consecutive_failures=0
- **Platform flaky:** Mine Worknet API returns 503 periodically (WS + HTTP poll claim + ready pool). Validator auto-retries with exponential backoff. Eventually reconnects.
- **Previous successful evals:** 2026-05-24 (10 eval rows, mostly score 100, one 85). 2026-05-25 had 0 completed evals so far.
- **Earlier patches applied:** MINE_PLATFORM_HTTP_TIMEOUT=90, MINE_WS_OPEN_TIMEOUT=30, MINE_LLM_MODE=cli.

### Ardinals minting (stopped)
- Ardinals cron/daemon stopped per Kevin request (low gas). Timer disabled.
- Last active wallet: `0x1105dcd09006F5e3e2905079a70968d1cAdE4a3c` (profile `ardi-current`).
- Stake: via `0x4355B5b345B50D2aCA18b30da041b43E19ba8172` into worknet 845300000014 (11K AWP).
- No active mint automation.

## Resume Snapshot
- Relevant wallet path: `/root/.openclaw-wallet`
- Relevant file: `/root/.openclaw-wallet/wallets/default/wallet.json`
- User wanted help around AWP rewards and export/sell flow.
- Secret export itself should not be performed by the assistant, but wallet location and non-secret workflow can be guided.

## Resume Questions
- which wallet/address is active now?
- are rewards claimable or already landed?
- what is the intended sell route once the user has exported/accessed the wallet safely?

## Working Notes

## 2026-05-08 — Ardinals minting switch to burn wallet
- Mine validator/miner stopped per Kevin request; focus moved fully to Ardinals minting.
- Active Ardinals burn/mint wallet switched to `0x8ABc1AB8682eE0A19526e42C228825eFda8C6468` (derived from user-provided burn PK; do not echo secret in chat).
- AWP/Ardi stake path currently works via delegated/self allocation from `0x4355B5b345B50D2aCA18b30da041b43E19ba8172` into Ardi worknet `845300000014`; `ardi-agent stake` reported ELIGIBLE for burn wallet via staker `0x4355...8172`.
- Burn wallet Base ETH was topped up above floor; recent observed balance ~0.0046-0.0047 ETH. Still low-ish; refill threshold warning remains 0.01 ETH, recommended cushion 0.05 ETH.
- Auto-mine runtime had infra issues after switch:
  - systemd PATH issue caused `hermes not on PATH`
  - attempted claude/openclaw-scripted runtimes also failed in this environment
  - manual tick with explicit PATH + Hermes works
  - timer config updated toward `AWP_AGENT_ID=ardi-burn`, MAX_PER_EPOCH=3, but daemon reliability needs re-check after context reset
- Ardinals results on burn wallet currently show 3 total inscribed wins:
  - token `#3665` from epoch `352`, wordId `3664`, answer `consensus`, power `89`, language `en` (Kevin later confirmed this NFT was transferred out and was legendary)
  - token `#11418` from epoch `353`, wordId `11417`, answer `서리`, power `65`, language `ko`
  - token `#12395` from epoch `421`, wordId `12394`, answer `想象`, power `64`, language `zh`
- Remaining mint cap after the third recorded inscribe: 2.
- Audit rule for fairness:
  - `lost` + `reveal_tx` present = fair VRF loss
  - `revealed` = reveal done, winner/inscribe not finalized yet
  - `committed` = unresolved; cannot claim pure VRF loss yet

## 2026-05-08 — Ardi timer skip bug found/patched
- Kevin correctly challenged claim that bot was merely standby: burn wallet last on-chain commit/reveal was ~07:06 UTC (epoch 703), then no new commits for ~5h; some previous stale reveal attempts failed.
- Root cause found in `ardi-tick.sh` precheck: it only detected `commit_deadline`, but `ardi-agent context` / coordinator now emits camelCase `commitDeadline`. Timer therefore skipped open epochs with `tick: no work`.
- Patched both symlink/live source to detect `commit_deadline|commitDeadline` and parse either. After patch, service saw open commit window and spawned runtime.
- Hermes and OpenClaw scripted runtimes are too slow/wedge for 180s commit windows. Switched `/root/.ardi-agent/auto-mine.env` to `ARDI_AGENT_RUNTIME=scripted` and added `/root/.local/share/ardi-auto-mine/runtime/scripted.sh`: deterministic no-LLM solver/driver for obvious riddles + pending reveal/inscribe. This is safer than waiting for LLM cold start.
- Current burn wallet remains `0x8ABc1AB8682eE0A19526e42C228825eFda8C6468`; gas `0.004435 ETH`, preflight/stake OK, remaining cap 2, mints still #3665/#11418/#12395.
- Need monitor next open epoch after 13:42 UTC to confirm scripted runtime actually commits when open. If no commits, improve precheck/runtime further.

## 2026-05-08 — New ardi-current wallet live
- Kevin provided new Ardinals wallet PK via attachment and said old burn wallet was deleted. Imported into awp-wallet profile `ardi-current`; do not expose secret.
- New active address: `0x1105dcd09006F5e3e2905079a70968d1cAdE4a3c`.
- Updated `/root/.ardi-agent/auto-mine.env`: `AWP_AGENT_ID=ardi-current`, `ARDI_AGENT_RUNTIME=scripted`.
- Preflight 5/5 passed. Stake eligible via staker `0x4355...8172`, 11,000 AWP allocated. Gas ~0.00443 ETH (low, below refill threshold 0.01, but above 0.003 floor). Agent state: 0 mints, remaining cap 5.
- Manual commit succeeded for epoch 747 wordId 11440 answer `임무`, commit tx `0x43fe69de5f9133957f20b41ea5c575fbf649d5f7f9f3f29178c18fbdefc0f894`.
- Timer/scripted reveal succeeded: reveal tx `0x64eb4ab2da4ae0977d05ea3cd4638f83823939605ad73a67a5c7a4c52a70f85e`. Status now `revealed` / inscribe pending. `inscribe` check said VRF pending with 39 correct revealers; retry inscribe after ~60s until won/lost.

## 2026-05-08 — Ardinals daemon stopped
- Kevin decided not to continue Ardinals cron because gas is low and wants to move to test validator.
- Stopped and disabled `ardi-mine.timer`; `ardi-mine.service` inactive/dead.
- Active env remains `AWP_AGENT_ID=ardi-current`, `ARDI_AGENT_RUNTIME=scripted` for reference, but timer is OFF.
- Current ardi-current state: one historical entry epoch 747 wordId 11440 answer `임무`, revealed tx `0x64eb4ab2da4ae0977d05ea3cd4638f83823939605ad73a67a5c7a4c52a70f85e`, final `lost`; no actionable pending.

## 2026-05-08 — Mine validator started
- Kevin assigned 11K AWP to Mine Worknet `84530000002` and provided validator PK via attachment; do not expose secret.
- First accidental validator start used default wallet `0x4355...8172`; stopped it.
- Started validator with direct PK signer. Validator address derived: `0x76F2B0e39D67CcC0c7d864c3d4bdE4dcfbfD2dA9`.
- `validator-doctor`: signer ok type `pk`, AWP registration ok, platform reachable; heartbeat got transient 503 but `can_start=true`.
- Validator session `validator-1778251500`, pid `2973833`, log `/root/.openclaw/skills/mine-skill/output/validator-runs/validator-1778251500.log`.
- Status: running, WebSocket connected, eligible yes, ready pool not joined yet/retrying on heartbeat, no tasks evaluated yet. No `validator_capacity_full` response observed.
- Env file for future runs: `/root/.openclaw/skills/mine-skill/.validator_pk.env` contains sensitive PK; chmod 600.

## 2026-05-08 — Mine validator application approved
- Investigation showed validator was still role `miner` because application was missing; `/api/iam/v1/validator-applications/me` returned `{}` and ready pool 403 required `mining.validator.ready`.
- Submitted validator application manually with validator PK signer. It auto-approved: app id `885`, status `approved`, address `0x76f2...2da9`, reviewed_by `auto`, submitted/reviewed `2026-05-08T16:24:22Z`.
- Restarted validator. `GET /api/iam/v1/me` now returns role `validator` for `0x76f2...2da9`.
- Runtime session now `validator-1778257535`, pid `2981839`. WebSocket connects, but ready/heartbeat often time out due Mine API crunkiness. Ready pool still not joined as of 16:28 UTC; no tasks yet. Important: no longer capacity_full/permission-role blocker; now platform timeout/WS instability is the blocker.

## 2026-05-08 — Mine validator active with tasks
- By ~19:27 UTC, Mine validator is active: ready pool joined, WS connected, eligible yes.
- Status: tasks_received 47, tasks_evaluated/reported 30, all 30 match, current active task `evt_01f05fc0-5487-4731-b778-9579b209b5bf`. Rewards still 0 until epoch settlement.
- Blocker now is PoW challenge quality: several AI reasoning PoW answers are wrong because LLM returns bootstrap/meta answers (`read bootstrap.md`). Some PoWs passed, but repeated failures caused cooldown (e.g. 960s). Need improve `_solve_logic_puzzle` prompt/sanitization or route PoW to better model if asked.

## 2026-05-08 — Mine validator PoW solver patched
- Kevin provided a self-contained `pow_solver` used for miner. Replaced `scripts/pow_solver.py` with uploaded version after backup `scripts/pow_solver.py.bak-20260508T1933`; py_compile OK.
- Patched `scripts/validator_runtime.py` `_handle_pow_challenge` to call `pow_solver.solve_challenge(challenge)` first, fall back to LLM only if deterministic solver fails.
- Hardened LLM fallback system prompt to ignore workspace/bootstrap/tool instructions and solve only puzzle.
- Restarted validator. New session `validator-1778268800`, pid `2997367`. Status remains running, ready pool joined, WS connected, 47 received / 30 reported so far.
- Need monitor next PoW to confirm deterministic solver passes and no more bootstrap.md answers.

## 2026-05-08 — Fixed temporary bot misuse
- Kevin complained temporary Telegram bot `@temporaryawp_bot` was getting validator notifications, but it should only monitor AWP token movement for contract `0xAB41eE5C44D4568aD802D104A6dAB1Fe09C590D1` and token `0x0000A1050AcF9DEA8af9c2E74f0D7CF43f1000A1`.
- Removed cron `mine_validator_watch.py` from crontab and backed it up as disabled; validator monitor no longer uses that bot.
- Rewrote `scripts/awp_contract_watch.py` to monitor both IN and OUT ERC-20 Transfer logs for the exact AWP token on Base, with large movement threshold 100,000 AWP.
- Switched watcher RPC to `https://base.drpc.org` after previous RPC timed out. Current state checked through block `45739845`, events 0. Cron remains hourly at minute 7 only for `awp_contract_watch.py`.
