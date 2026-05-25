# TWITTERSNIPPERMEMORY.md
- Title: TwitterSnipper / Alpha + NFT X discovery bots
- Kind: workflow / project
- Status: active

## ⚡ COMPACT-SAFE STATUS (read this first after any reset/compaction)
**Last updated:** 2026-05-25T18:43Z

**Public backup repo:** https://github.com/papatora/TwitterSnipper
- Latest backup commit: `21282b2 Backup sanitized Twitter sniper scripts`
- Sanitized: no cookies, no Notion tokens, no Frontrun token, no API keys, no proxy creds, no runtime state/log/cache.
- Use GitHub SSH key: `/root/.ssh/id_ed25519_github_backup` with `GIT_SSH_COMMAND="ssh -i /root/.ssh/id_ed25519_github_backup -o IdentitiesOnly=yes"`.

## Alpha account scout
- Live script: `/root/.openclaw/workspace/skills/alpha_account_scout/scripts/scout.py`
- Revert script used earlier: `/root/.openclaw/workspace/backups/alpha_revert_20260522T134832Z/REVERT.sh`
- Alpha patch from 2026-05-22 was reverted on 2026-05-24 because quality worsened over two days.
- Then Kevin supplied new reference/rules/auth; imported 67 seed handles, added 20 new to config, updated rules, cookie pool total 46.
- Alpha Notion DB IDs (no tokens here): Main `99aaf8afdaf34eb79793d6b9518a922a`; Reject `33e878f48f9880a89558d548026ac91e`.

## NFT Twitter bot
- Live dir: `/root/.openclaw/workspace/NFTtwitterbot/NFTtwitterbot`
- Main script: `nft_sniper.py`
- Cron every 20 minutes:
  `*/20 * * * * cd /root/.openclaw/workspace/NFTtwitterbot/NFTtwitterbot && /usr/bin/flock -n /tmp/nft_sniper.lock /usr/bin/timeout 18m /usr/bin/python3 nft_sniper.py >> sniper_live.log 2>&1`
- No LLM/no-cost; uses Python `requests` only.
- Main NFT DB: `ccb633a6-5089-4982-a158-c479864b72f9`; Reject DB: `33e878f48f9880a89558d548026ac91e`.
- Current seed count: 40 unique handles (was 25, added 15 fresh NFT seeds 2026-05-25).
- Monitor file: `runtime/last_run_status.json` with WIB timestamp.

### NFT bot important fixes
- Fixed parser so comments in `nftseed.txt` are ignored; cleaned 13 junk seed-state keys.
- Restored `FOLLOWERS_CAP=1000` for NFT bot (not 500), matching docs (>1k hard reject).
- Added stronger NFT project signals: supply numbers (`10K PFP`, `10,000 NFTs`, `333 Cubs`), mint soon/live, launch/genesis, art collection, opensea/zora/manifold, NFT tooling (`mint calendar`, analytics/tooling), chain+collection phrasing.
- Discord or generic `onchain` alone is not enough to pass.
- Known classifier sanity: `kikicubseth` => nft; `wtf81` => ct_only; `centralbankmeme` => ct_only; `uapenfts` => nft; `DailyMintsAI` => nft.
- Added periodic rescan safety valve so old broken gate/diff state cannot bury candidates forever.
- `upsert_reject_notion()` now dedupe-on-create/update; no blind duplicate rows.
- Fixed reject DB counter and duplicate logging.

### NFT cleanup/rescue results
- Archived 13 bad smoke-test Main rows: unstonio, fadu1369, Bubble_nft_, Nzubechukw46, Robertonrr1, APEFLTR, Galactoid101, vhya213, solanadrop012, wtf81, Billini_, piecebyjulian, CanWinLounge.
- Snapshot backfill inserted: KikiCubsETH, ednovart, gemsopfp, ArchonWorld, AninamiaNFT, AqualotsNFT, Tabcorp_HR, PNAGrade, v0punk, citizensonchain.
- Reject DB dedupe: 1270 total rows -> archived 409 duplicate rows; 218 handles had duplicates; ~851 unique active left.
- Reject rescue inserted: dailymintsai, uapenfts; archived their reject rows.
- Bad intermediate `@@handle` Main rows (9 rows) were archived; do not trust them.
- Final active Main NFT rows reported: 18.

## Open issues / next steps
- NFT discovery still limited by seed following graph. Added 15 fresh seeds (40 total). If main still low, add fallback search lane.
- Consider fallback search queries: `mint soon`, `genesis collection`, `onchain art`, `pfp collection`, `ethereum nft launching`, `base nft minting`, `solana pfp collection`, `opensea launching`, `zora collection`.
- Keep precision: reject personal NFT collectors/community managers unless project/tooling signal is strong.
- Alpha scout Main still gets noisy from TAO self-reinforcing graph. Gates tightened 2026-05-25 but will need ongoing guard.
- Re-enable search lanes when cookies/xreach tweet rate limits are manageable.

## 2026-05-25 alpha scout calibration (PM session)
### xreach fix
- Patched xreach parser for Twitter API shape change (core.* fields).
- Updated query IDs, feature payloads, scout.py to skip numeric ID lookups.

### Gate fixes to fix TAO dominance + false rejects
- `group_balanced_seed_order`: pushed tao_focus from fixed_head to secondary (after infra_market).
- `precision_gate`: added `obvious_project_rescue` for Bittensor/infra builders (projectish>=2 + individual<=1).
- Added `tao_personal_noise_not_project` guard (catches "I hold TAO", "all in TAO", personal calls) before precision gate.
- Made `individual_profile_not_project` exempt obvious_project_rescue.
- Archived 8 dirty rows from Main Notion.
- Cron still: ALPHA_SKIP_THESIS_PREFLIGHT=1, ALPHA_AUTOLEARN_SEEDS=1.

### NFT bot seed expansion
- Added 15 fresh NFT seeds found via xreach search mention mining.
- Seed count: 25 → 40.
- Fresh seeds prioritized first in sort order.
- Added `Handle` field to Notion create/update (was missing).
- Rescued DumeriDao from Reject DB → NFT Main.

### Known issues
- UserByRestId (xreach user <numeric_id>) still fails with Cloudflare 403.
- Seed following graph still main discovery lane; search lanes disabled.
- Patched `/root/.openclaw/workspace/node_modules/xreach-cli/dist/lib/client/users.js` and source `src/lib/client/users.ts` to parse Twitter's new user shape: `core.name`, `core.screen_name`, `core.created_at`, `avatar.image_url`, `privacy.protected`, `verification.verified`, while retaining legacy fallbacks.
- Updated xreach query ID cache at `~/.config/xfetch/query-ids.json`; current Following QID `zicR0BDMP2j83QqeDjky9A`, UserByScreenName `IGgvgiOx4QZndDHuD3x9TQ`, UserByRestId `VQfQ9wwYdk6j_u2O4vt64Q`.
- Patched xreach `dist/lib/client/base.js` to send per-operation feature payloads for UserByScreenName/UserByRestId rather than broad default features.
- Verified following now works again for active seed handles: lookonchain, MagicEden, PhalaNetwork, rookieXBT, chronosontempo. Earlier 2/19 success improved to 12/19; remaining tested failures were truly unavailable handles or UserByRestId limitation.
- UserByRestId (`xreach user <numeric_id>`) still returns Twitter/Cloudflare HTML 403 in xreach despite correct QID/features; manual API can work, but scout should not depend on numeric user hydration.
- Patched `/root/.openclaw/workspace/skills/alpha_account_scout/scripts/scout.py` `hydrate_search_user()` to avoid numeric-only `xreach user <restId>` lookups; it now only hydrates by screenName and skips numeric-only payloads so the user_lookup circuit breaker no longer kills seed graph runs.
- Manual scout verification after patch: no user_lookup_circuit_breaker/seed_fail cascade; run spent time in search preflight and hit tweet rate limits, finalized cleanly with 0 new and `seeds_scanned=0` because deadline was consumed before seed scan. Next run should be monitored to confirm seed scanning resumes now following parser is fixed.
