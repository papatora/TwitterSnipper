#!/usr/bin/env python3
import os
import json
import subprocess
import datetime
import urllib.request
import unicodedata
import urllib.error
import re
import time
import random
import signal
from pathlib import Path

STATE_PATH = "/root/.openclaw/workspace/memory/alpha_account_state.json"
RUN_LOG_PATH = "/root/.openclaw/workspace/memory/alpha_scout_runs.jsonl"
LATEST_LOG_PATH = "/root/.openclaw/workspace/memory/alpha_scout_latest.log"
OTHERS_ALPHA_PATH = "/root/.openclaw/workspace/othersalpha.txt"
SEEDS_CONFIG_PATH = "/root/.openclaw/workspace/skills/alpha_account_scout/config/seeds.json"
SEED_GROUPS_DIR = "/root/.openclaw/workspace/skills/alpha_account_scout/config/seed_groups"
REFERENCE_SETS_DIR = "/root/.openclaw/workspace/skills/alpha_account_scout/config/reference_sets"
NOTION_ENV = "/root/.openclaw/workspace/.secrets/notion.env"
CHINA_NOTION_ENV = "/root/.openclaw/workspace/.secrets/china_alpha_notion.env"
REJECT_NOTION_SOURCE = "/root/.openclaw/workspace/.secrets/reject_notion.env"
BYFOLLOWER_NOTION_ENV = "/root/.openclaw/workspace/.secrets/byfollower_notion.env"
TWITTER_PROXY_ENV = "/root/.openclaw/workspace/.secrets/twitter_proxy.env"
BLOCKED_HANDLES_PATH = "/root/.openclaw/workspace/memory/alpha_blocked_handles.json"
KEEP_HANDLES_PATH = "/root/.openclaw/workspace/memory/alpha_keep_handles.json"
BLOCKED_HANDLES = set()
KEEP_HANDLES = set()
TWITTER_ENV = "/root/.openclaw/workspace/.secrets/twitter_cookies.env"
TWITTER_POOL_ENV = "/root/.openclaw/workspace/.secrets/twitter_cookies_pool.json"
XREACH = "/root/.openclaw/workspace/node_modules/.bin/xreach"
FRONTRUN_DIR = "/root/frontrun-cli"
FRONTRUN_CMD = ["node", "/root/frontrun-cli/src/cli.js", "overview"]
FRONTRUN_CACHE_PATH = "/root/.openclaw/workspace/memory/frontrun_cache.json"
MAX_PREFLIGHT_FRONTRUN_PER_RUN = int(os.environ.get("ALPHA_MAX_PREFLIGHT_FRONTRUN_PER_RUN", "5"))
PREFLIGHT_FRONTRUN_COUNT = 0

def normalize_xreach_user(u):
    if not isinstance(u, dict):
        return u
    out = dict(u)
    if out.get("description") is None and out.get("bio") is not None:
        out["description"] = out.get("bio")
    if out.get("followersCount") is None and out.get("followers") is not None:
        out["followersCount"] = out.get("followers")
    if out.get("friendsCount") is None and out.get("following") is not None:
        out["friendsCount"] = out.get("following")
    return out

def select_rejects_for_push(records, limit):
    """Pick rejects fairly across source/reason so Notion reject DB reflects broad scan, not first noisy seed."""
    if limit <= 0 or len(records) <= limit:
        return list(records)
    buckets = {}
    order = []
    for rec in records:
        source = (rec.get("source") or rec.get("source_seed") or "unknown").strip().lower() or "unknown"
        reason = (rec.get("reason") or "unknown").strip().lower() or "unknown"
        if source.startswith("search:"):
            # keep query identity but avoid one query monopolizing all pushes
            parts = source.split(":")
            key = ":".join(parts[:3]) if len(parts) >= 3 else source
        elif source.startswith("following:@"):
            key = source.split(":", 1)[1]
        else:
            key = source.split(":", 1)[0]
        bucket_key = (key, reason)
        if bucket_key not in buckets:
            buckets[bucket_key] = []
            order.append(bucket_key)
        buckets[bucket_key].append(rec)
    picked = []
    idx = 0
    while len(picked) < limit and buckets:
        key = order[idx % len(order)]
        q = buckets.get(key) or []
        if q:
            picked.append(q.pop(0))
            if not q:
                buckets.pop(key, None)
                order = [k for k in order if k in buckets]
                if not order:
                    break
                idx = idx % len(order)
                continue
        idx += 1
    return picked

MIN_NEW_PER_RUN = 10
MAX_NEW_PER_RUN = 25
SEARCH_FAIL_FAST_THRESHOLD = 12
SEARCH_PRIMARY_N = 20
SEARCH_RETRY_N = 30
FAILED_USER_LOOKUPS = set()
DISABLED_SECOND_HOP_SOURCES = {
    "IBuildMokee", "adonis_ds", "polska742", "MemeSnipeAI", "PunnyPumper",
    "CrypOracleSOL", "Skai07318090073", "TheVexDex", "HiveGuardPro",
    "EduJames525", "DLMMerfun"
}
USER_LOOKUP_ATTEMPTS = 0
USER_LOOKUP_FAILURES = 0
MAX_USER_LOOKUP_ATTEMPTS = 18
MAX_USER_LOOKUP_FAILURES = 8
USER_LOOKUPS_DISABLED = False
MAX_TWEET_SIGNAL_LOOKUPS = 24
TWEET_SIGNAL_LOOKUP_COUNT = 0
TWEET_SIGNAL_CACHE = {}
TWEET_SIGNAL_DISABLED = False
RATE_LIMIT_HITS = 0
RATE_LIMIT_THRESHOLD = 50
XREACH_RATE_LIMITED = False
SOFT_RATE_LIMIT_MODE = False
TWITTER_POOL = None
TWITTER_POOL_INDEX = 0
BAD_TWITTER_POOL_LABELS = set()
BAD_TWITTER_POOL_STRIKES = {}  # label -> int, require 3 strikes before marking bad
BAD_COOKIE_STRIKE_THRESHOLD = 3
BAD_FOLLOWING_SUBJECTS = set()
blocked_handles = set()
keep_handles = set()
lane_known_counts = {}

# xreach stability tuning: only timeout/retry/pool rotation behavior.
# Discovery lanes / seeds / query set must stay untouched unless explicitly requested.
XREACH_BASE_TIMEOUT_S = 45
XREACH_USER_TIMEOUT_S = 10
XREACH_TWEETS_TIMEOUT_S = 8
XREACH_SEARCH_TIMEOUT_S = int(os.environ.get("XREACH_SEARCH_TIMEOUT_S", "25"))
XREACH_FOLLOWING_TIMEOUT_S = int(os.environ.get("XREACH_FOLLOWING_TIMEOUT_S", "12"))
# Keep cron runs inside OpenClaw's execution window. Search is the noisiest lane
# and often needs slightly more patience plus one retry/cookie rotation to avoid
# collapsing to 12/12 query failures during transient xreach degradation.
XREACH_TIMEOUT_RETRIES = int(os.environ.get("XREACH_TIMEOUT_RETRIES", "1"))
XREACH_TRANSIENT_RETRIES = int(os.environ.get("XREACH_TRANSIENT_RETRIES", "2"))
XREACH_MAX_POOL_PASSES = int(os.environ.get("XREACH_MAX_POOL_PASSES", "1"))
XREACH_SEARCH_MAX_POOL_PASSES = int(os.environ.get("XREACH_SEARCH_MAX_POOL_PASSES", "2"))
XREACH_TIMEOUT_BACKOFF_S = 1.0
RUN_DEADLINE_S = int(os.environ.get("ALPHA_SCOUT_DEADLINE_S", "600"))
FINALIZE_DEADLINE_BUFFER_S = 180
# Keep the post-scan Notion reject push bounded. The run was finishing discovery
# around the 570s soft deadline but still got OpenClaw SIGTERM before summary
# persistence when it tried to push hundreds of rejects. Prefer a stable summary
# over exhaustive reject-sync during degraded xreach windows.
MAX_REJECTS_PUSH_PER_RUN = int(os.environ.get("ALPHA_MAX_REJECTS_PUSH_PER_RUN", "80"))
XREACH_FAILURE_STOP_THRESHOLD = int(os.environ.get("XREACH_FAILURE_STOP_THRESHOLD", "10"))
XREACH_CONSECUTIVE_FAILURES = 0



def load_env(path):
    env = {}
    for line in open(path):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            k, v = line.split('=', 1)
            env[k] = v
    return env


def load_twitter_proxy():
    proxy = os.environ.get("TWITTER_PROXY", "").strip()
    if proxy:
        return proxy
    try:
        if os.path.exists(TWITTER_PROXY_ENV):
            return (load_env(TWITTER_PROXY_ENV).get("TWITTER_PROXY", "") or "").strip()
    except Exception as e:
        print(f"[scout] twitter proxy env load failed: {e}")
    return ""


def should_use_twitter_proxy_for_xreach():
    mode = os.environ.get("ALPHA_TWITTER_PROXY_MODE", "always").strip().lower()
    if mode in {"always", "1", "true", "yes"}:
        return True
    if mode in {"off", "0", "false", "no"}:
        return False
    # Default: proxy is fallback only after signs of limit / cookie-pool trouble.
    return bool(RATE_LIMIT_HITS > 0 or SOFT_RATE_LIMIT_MODE or XREACH_RATE_LIMITED or BAD_TWITTER_POOL_LABELS or XREACH_CONSECUTIVE_FAILURES >= 8)


def load_twitter_pool():
    global TWITTER_POOL
    if TWITTER_POOL is not None:
        return TWITTER_POOL
    pool = []
    if os.path.exists(TWITTER_POOL_ENV):
        try:
            data = json.load(open(TWITTER_POOL_ENV))
            for item in data:
                if item.get("auth_token") and item.get("ct0"):
                    pool.append(item)
        except Exception:
            pool = []
    if not pool and os.path.exists(TWITTER_ENV):
        env = load_env(TWITTER_ENV)
        if env.get("auth_token") and env.get("ct0"):
            pool.append({"label": "single", "auth_token": env["auth_token"], "ct0": env["ct0"]})
    TWITTER_POOL = pool
    return TWITTER_POOL


def next_twitter_creds(advance=False):
    global TWITTER_POOL_INDEX
    pool = load_twitter_pool()
    if not pool:
        raise RuntimeError("No twitter cookies available")
    if advance:
        TWITTER_POOL_INDEX = (TWITTER_POOL_INDEX + 1) % len(pool)
    if not BAD_TWITTER_POOL_LABELS:
        return pool[TWITTER_POOL_INDEX]
    for _ in range(len(pool)):
        cand = pool[TWITTER_POOL_INDEX]
        label = cand.get("label", f"idx{TWITTER_POOL_INDEX}")
        if label not in BAD_TWITTER_POOL_LABELS:
            return cand
        TWITTER_POOL_INDEX = (TWITTER_POOL_INDEX + 1) % len(pool)
    return pool[TWITTER_POOL_INDEX]


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"handles": {}}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"handles": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def append_othersalpha(records):
    if not records:
        return
    now = datetime.datetime.now(datetime.UTC).isoformat()
    lines = [f"\n## Run {now}\n"]
    seen = set()
    for rec in records:
        handle = rec.get("handle") or ""
        source = rec.get("source") or ""
        followers = rec.get("followers")
        reason = rec.get("reason") or ""
        bio = (rec.get("bio") or "").replace("\n", " ").strip()
        key = (handle.lower(), source, reason)
        if not handle or key in seen:
            continue
        seen.add(key)
        line = f"- @{handle}"
        if followers is not None:
            line += f" | followers={followers}"
        if source:
            line += f" | source={source}"
        if reason:
            line += f" | reason={reason}"
        if bio:
            line += f" | bio={bio[:280]}"
        lines.append(line)
    if len(lines) == 1:
        return
    with open(OTHERS_ALPHA_PATH, "a") as f:
        f.write("\n".join(lines) + "\n")


def should_record_othersalpha(cand):
    if not isinstance(cand, dict) or not cand.get("rejected"):
        return False
    reason = cand.get("reason") or ""
    return reason not in {"known_existing", "missing_followers"}


def append_run_summary(summary):
    try:
        os.makedirs(os.path.dirname(RUN_LOG_PATH), exist_ok=True)
        with open(RUN_LOG_PATH, "a") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        with open(LATEST_LOG_PATH, "w") as f:
            f.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        try:
            dashboard_hook = "/root/.openclaw/workspace/scripts/update_alpha_scout_dashboard.sh"
            if os.path.exists(dashboard_hook):
                subprocess.Popen(["/usr/bin/bash", dashboard_hook], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as hook_err:
            print(f"[scout] dashboard hook failed: {hook_err}")
    except Exception as e:
        print(f"[scout] run summary append failed: {e}")


def should_autopromote_othersalpha(rec):
    if not isinstance(rec, dict):
        return False
    reason = (rec.get("reason") or "").strip()
    bio = (rec.get("bio") or "").lower()
    source = (rec.get("source") or "").lower()
    followers = rec.get("followers")
    try:
        followers = int(followers)
    except Exception:
        followers = None

    if followers is None:
        return False
    if reason in {"blacklisted_location", "non_crypto_profile"}:
        return False

    strong_phrases = [
        "prediction market", "polymarket", "crypto-native", "non-custodial crypto wallet",
        "non-custodial crypto wallets", "wallet", "launchpad", "on @tempo", "on tempo",
        "on @solana", "on solana", "blockchain", "rollup settlement", "l1 blockchain",
        "sol wagering", "token protocol", "bittensor", "subnet", "tao", "base",
        "smart contract", "hyperliquid", "defi", "onchain", "spl", "sui", "bnb chain"
    ]
    weak_bad_phrases = [
        "build in public", "saas", "clinical", "therapy", "meditation", "real estate",
        "football", "gamer", "yoga", "newsletter", "lawyer", "jesus lover", "aesthetic"
    ]
    strong_hits = sum(1 for p in strong_phrases if p in bio)
    weak_bad_hits = sum(1 for p in weak_bad_phrases if p in bio)
    seeded_crypto_source = any(tag in source for tag in ["tempo", "tao", "infra_market", "prediction", "market", "wallet", "polymarket", "b4kenny", "m0ment0_", "notab2d_", "rightpitch20", "xweth_", "yangxiao214", "subit_crypto", "sui", "bnb"])
    if "search:solana_plays:" in source or "autopromote:search:solana_plays:" in source:
        return False
    if "search:tempo:" in source and (not bio or len(bio.strip()) < 12 or strong_hits < 2):
        return False
    if "search:tao_subnet:" in source and (not bio or len(bio.strip()) < 12 or strong_hits < 2):
        return False
    if "search:arc_base:" in source and (not bio or len(bio.strip()) < 12 or strong_hits < 2):
        return False
    if "search:ai_general:" in source and (not bio or len(bio.strip()) < 12 or strong_hits < 2):
        return False
    if ("bitcoin" in bio or "btc" in bio) and all(x not in bio for x in ["wallet", "payments", "onchain", "defi", "market", "protocol"]):
        return False

    if strong_hits >= 2 and weak_bad_hits == 0:
        return True
    if strong_hits >= 1 and seeded_crypto_source and weak_bad_hits == 0:
        return True
    if any(p in bio for p in ["capital markets", "conversational finance", "tradable assets", "built on @base", "on @bnbchain", "on bnb chain"]) and weak_bad_hits == 0:
        return True
    if reason in {"spam_keywords", "insufficient_crypto_proof", "generic_builder_without_crypto", "empty_bio_without_strong_crypto_proof"} and strong_hits >= 1 and weak_bad_hits == 0:
        return True
    if reason in {"weak_random_personal_profile", "empty_bio_without_strong_crypto_proof"} and followers <= 120 and seeded_crypto_source and weak_bad_hits == 0:
        return True
    if not bio and reason == "empty_bio_without_strong_crypto_proof" and followers <= 80 and seeded_crypto_source:
        return True
    return False


def run_xreach(args):
    global RATE_LIMIT_HITS, XREACH_RATE_LIMITED, SOFT_RATE_LIMIT_MODE, BAD_FOLLOWING_SUBJECTS, XREACH_CONSECUTIVE_FAILURES
    op = args[0] if args else ""
    is_expensive_lookup = op in {"user", "tweet", "tweets", "following", "search"}
    subject = args[1] if len(args) > 1 else ""
    if op == "following" and subject in BAD_FOLLOWING_SUBJECTS:
        print(f"[checkpoint] following_subject_quarantined | @{subject}")
        return None
    if XREACH_RATE_LIMITED:
        return None
    pool = load_twitter_pool()
    attempts = max(1, len(pool)) * max(1, XREACH_MAX_POOL_PASSES)
    if op == "following":
        attempts = min(attempts, min(2, max(1, len(pool))))
    elif op == "search":
        attempts = min(max(1, len(pool)) * max(1, XREACH_SEARCH_MAX_POOL_PASSES), min(6, max(2, len(pool))))
    transient_markers = [
        "unexpected end of json input",
        "doctype html",
        "is not valid json",
        "internal server error",
        "econnreset",
        "etimedout",
        "socket hang up",
        "tls handshake timeout",
    ]
    # These indicate the target account is suspended/deleted/private — never retry.
    subject_error_markers = [
        "twitter api error (http 404)",
        '"code": "not_found"',
        "twitter api error (http 0)",
        "strconv.parseint: parsing \"\": invalid syntax",
        "invalid syntax",
        "cannot read properties of undefined (reading 'name')",
        "cannot read properties of undefined (reading 'result')",
        "cannot read properties of undefined",
    ]
    bad_cookie_markers = [
        "cookie extraction failed",
        "unauthorized",
        "forbidden",
    ]

    def timeout_for(op_name):
        if op_name == "following":
            return XREACH_FOLLOWING_TIMEOUT_S
        if op_name == "search":
            return XREACH_SEARCH_TIMEOUT_S
        if op_name == "tweets":
            return XREACH_TWEETS_TIMEOUT_S
        if op_name in {"user", "tweet"}:
            return XREACH_USER_TIMEOUT_S
        return XREACH_BASE_TIMEOUT_S

    timeout_s = timeout_for(op)
    for attempt in range(attempts):
        advance = attempt > 0 and len(pool) > 1
        tw = next_twitter_creds(advance=advance)
        label = tw.get("label", f"idx{TWITTER_POOL_INDEX}")
        cmd = [XREACH, "--auth-token", tw["auth_token"], "--ct0", tw["ct0"]] + args
        timeout_retries_left = XREACH_TIMEOUT_RETRIES
        transient_retries_left = XREACH_TRANSIENT_RETRIES

        while True:
            try:
                proc_env = None
                proxy = load_twitter_proxy()
                if proxy and should_use_twitter_proxy_for_xreach():
                    proc_env = os.environ.copy()
                    proc_env["TWITTER_PROXY"] = proxy
                    proc_env.setdefault("HTTP_PROXY", proxy)
                    proc_env.setdefault("HTTPS_PROXY", proxy)
                    if RATE_LIMIT_HITS == 1:
                        print("[checkpoint] twitter_proxy_fallback_enabled | reason=rate_limit_or_cookie_pressure")
                proc = subprocess.Popen(
                    cmd,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    env=proc_env,
                )
                try:
                    out, stderr = proc.communicate(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except Exception:
                        pass
                    try:
                        out, stderr = proc.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except Exception:
                            pass
                        out, stderr = proc.communicate()
                    raise subprocess.TimeoutExpired(cmd, timeout_s, output=out, stderr=stderr)
                if proc.returncode != 0:
                    raise subprocess.CalledProcessError(proc.returncode, cmd, output=out, stderr=stderr)
                if RATE_LIMIT_HITS > 0:
                    RATE_LIMIT_HITS = max(0, RATE_LIMIT_HITS - 1)
                XREACH_CONSECUTIVE_FAILURES = 0
                parsed = json.loads(out)
                if isinstance(parsed, dict) and parsed.get("ok") is True and "data" in parsed:
                    data = parsed.get("data")
                    if isinstance(data, list):
                        if data and all(isinstance(x, dict) for x in data):
                            return {"items": [normalize_xreach_user(x) for x in data]}
                        return {"items": data}
                    if isinstance(data, dict):
                        return normalize_xreach_user(data)
                return parsed
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or "").strip()
                stdout = (e.output or "").strip()
                combined = "\n".join(x for x in [stdout, stderr] if x).strip()
                tail = combined[-700:] if combined else ""
                lower_tail = tail.lower()
                if any(marker in lower_tail for marker in subject_error_markers):
                    if op == "search":
                        print(f"[checkpoint] xreach_search_subject_error | account={label} | query={subject[:120]}")
                        break
                    if op == "following" and subject:
                        BAD_FOLLOWING_SUBJECTS.add(subject)
                        print(f"[checkpoint] following_subject_bad | @{subject} | account={label}")
                    else:
                        print(f"[checkpoint] xreach_subject_error | account={label} | op={op} | subject={subject[:120]}")
                    # xreach sometimes reports strconv.ParseInt/HTTP 0 for specific broken
                    # subjects. For following-user lookups this usually means a bad account.
                    # For search queries, do not quarantine the query text as if it were a handle.
                    # Do NOT count subject errors as consecutive failures — these are dead accounts,
                    # not infra/rate-limit issues. Circuit breaker should only trip on real failures.
                    if is_expensive_lookup:
                        print(f"[checkpoint] xreach_subject_skip | op={op} | subject=@{subject[:40]} | reason=dead_account")
                    # Subject error = upstream/xreach issue, not necessarily cookie issue.
                    # Return immediately instead of trying more cookies for the same subject.
                    return None
                if any(marker in lower_tail for marker in bad_cookie_markers):
                    should_mark_bad_cookie = op in {"user", "tweet", "tweets", "following"}
                    if should_mark_bad_cookie:
                        BAD_TWITTER_POOL_STRIKES[label] = BAD_TWITTER_POOL_STRIKES.get(label, 0) + 1
                        if BAD_TWITTER_POOL_STRIKES[label] >= BAD_COOKIE_STRIKE_THRESHOLD:
                            BAD_TWITTER_POOL_LABELS.add(label)
                            print(f"[checkpoint] xreach_bad_cookie | account={label} | op={op} | strikes={BAD_TWITTER_POOL_STRIKES[label]}")
                        else:
                            print(f"[checkpoint] xreach_cookie_strike | account={label} | op={op} | strikes={BAD_TWITTER_POOL_STRIKES[label]}/{BAD_COOKIE_STRIKE_THRESHOLD}")
                    else:
                        print(f"[checkpoint] xreach_search_notfound | account={label} | op={op}")
                    break
                if "rate limit exceeded" in lower_tail or "temporarily locked" in lower_tail or "try again later" in lower_tail:
                    RATE_LIMIT_HITS += 1
                    print(f"[scout] xreach rate_limited: {' '.join(args)} | account={label} | hit={RATE_LIMIT_HITS}")
                    if RATE_LIMIT_HITS >= max(6, len(pool)):
                        SOFT_RATE_LIMIT_MODE = True
                    break
                if any(marker in lower_tail for marker in transient_markers):
                    if transient_retries_left > 0:
                        print(f"[scout] xreach transient error: {' '.join(args)} | account={label} | retrying")
                        transient_retries_left -= 1
                        time.sleep(1.2)
                        continue
                    print(f"[checkpoint] xreach_transient_skip | {' '.join(args)} | account={label}")
                    break
                if tail:
                    print(f"[scout] xreach error: {' '.join(args)} | account={label} :: {tail}")
                else:
                    print(f"[scout] xreach error: {' '.join(args)} | account={label} :: exit={e.returncode}")
                break
            except json.JSONDecodeError as e:
                if transient_retries_left > 0:
                    print(f"[scout] xreach json decode transient for {' '.join(args)} | account={label} | retrying")
                    transient_retries_left -= 1
                    time.sleep(1.2)
                    continue
                print(f"[scout] xreach json decode error for {' '.join(args)} | account={label}: {e}")
                break
            except subprocess.TimeoutExpired:
                if timeout_retries_left > 0:
                    print(f"[scout] xreach timeout retry | {' '.join(args)} | account={label} | timeout={timeout_s}s")
                    timeout_retries_left -= 1
                    time.sleep(XREACH_TIMEOUT_BACKOFF_S)
                    continue
                if op == 'following' and subject:
                    BAD_FOLLOWING_SUBJECTS.add(subject)
                if is_expensive_lookup:
                    XREACH_CONSECUTIVE_FAILURES += 1
                    print(f"[checkpoint] xreach_timeout_skip | {' '.join(args)} | account={label} | timeout={timeout_s}s | consecutive_failures={XREACH_CONSECUTIVE_FAILURES}/{XREACH_FAILURE_STOP_THRESHOLD}")
                else:
                    print(f"[scout] xreach timeout: {' '.join(args)} | account={label} | timeout={timeout_s}s")
                break
            except Exception as e:
                lower_msg = str(e).lower()
                if any(marker in lower_msg for marker in transient_markers) and transient_retries_left > 0:
                    print(f"[scout] xreach transient exception: {' '.join(args)} | account={label} | retrying")
                    transient_retries_left -= 1
                    time.sleep(1.2)
                    continue
                print(f"[scout] exception for {' '.join(args)} | account={label}: {e}")
                break

        if is_expensive_lookup:
            XREACH_CONSECUTIVE_FAILURES += 1
            if XREACH_CONSECUTIVE_FAILURES >= XREACH_FAILURE_STOP_THRESHOLD:
                XREACH_RATE_LIMITED = True
                print(f"[checkpoint] xreach_circuit_breaker | consecutive_failures={XREACH_CONSECUTIVE_FAILURES}")
                return None
        if RATE_LIMIT_HITS >= RATE_LIMIT_THRESHOLD:
            XREACH_RATE_LIMITED = True
            print(f"[checkpoint] xreach_circuit_breaker | rate_limit_hits={RATE_LIMIT_HITS}")
            return None
        if len(BAD_TWITTER_POOL_LABELS) >= len(pool) and pool:
            # Treat an exhausted cookie pool as a global unavailable state, not just
            # a per-call failure. Without this, later search/retry lanes can keep
            # timing out until the outer OpenClaw exec SIGTERMs the run before the
            # normal finish summary is persisted.
            XREACH_RATE_LIMITED = True
            print(f"[checkpoint] xreach_cookie_pool_exhausted | bad={len(BAD_TWITTER_POOL_LABELS)} | pool={len(pool)}")
            print(f"[checkpoint] xreach_circuit_breaker | reason=cookie_pool_exhausted")
            return None
        if attempt + 1 < attempts:
            continue
        return None
    return None


def hydrate_search_user(u, tweet_id=None):
    global USER_LOOKUP_ATTEMPTS, USER_LOOKUP_FAILURES, USER_LOOKUPS_DISABLED
    if not isinstance(u, dict):
        return {}
    if u.get("screenName") and u.get("followersCount") is not None:
        return u
    if USER_LOOKUPS_DISABLED or MAX_USER_LOOKUP_ATTEMPTS <= 0:
        return u

    # Search results often only contain restId/id. Expand via tweet first to get
    # screenName, then do a full `user <handle>` lookup for followers/bio.
    if not u.get("screenName") and tweet_id:
        tw = run_xreach(["tweet", str(tweet_id), "--json"])
        if isinstance(tw, dict):
            tw_user = (tw.get("user") or {})
            if isinstance(tw_user, dict) and tw_user.get("screenName"):
                u = {**u, **tw_user}
                if u.get("followersCount") is not None:
                    return u

    # xreach 0.3.3 `user <numeric rest_id>` currently hits Twitter's
    # UserByRestId endpoint, which returns Cloudflare HTML/403 even with fresh
    # query IDs. Do not spend the global user-lookup budget on numeric-only
    # search payloads; only hydrate by screenName. The following endpoint already
    # returns complete user objects after our local xreach parser patch.
    lookup = u.get("screenName")
    if not lookup:
        return u
    key = str(lookup).lstrip("@")
    if key in FAILED_USER_LOOKUPS:
        return u
    if USER_LOOKUP_ATTEMPTS >= MAX_USER_LOOKUP_ATTEMPTS or USER_LOOKUP_FAILURES >= MAX_USER_LOOKUP_FAILURES:
        USER_LOOKUPS_DISABLED = True
        print(f"[checkpoint] user_lookup_circuit_breaker | attempts={USER_LOOKUP_ATTEMPTS} | failures={USER_LOOKUP_FAILURES}")
        return u
    USER_LOOKUP_ATTEMPTS += 1
    full = run_xreach(["user", key, "--json"])
    if isinstance(full, dict):
        return normalize_xreach_user(full)
    print(f"[checkpoint] user_lookup_skip | key={key} | attempts={USER_LOOKUP_ATTEMPTS} | failures={USER_LOOKUP_FAILURES + 1}")
    FAILED_USER_LOOKUPS.add(key)
    USER_LOOKUP_FAILURES += 1
    if USER_LOOKUP_ATTEMPTS >= MAX_USER_LOOKUP_ATTEMPTS or USER_LOOKUP_FAILURES >= MAX_USER_LOOKUP_FAILURES:
        USER_LOOKUPS_DISABLED = True
        print(f"[checkpoint] user_lookup_circuit_breaker | attempts={USER_LOOKUP_ATTEMPTS} | failures={USER_LOOKUP_FAILURES}")
    return u




def load_blocked_handles():
    try:
        if os.path.exists(BLOCKED_HANDLES_PATH):
            data = json.load(open(BLOCKED_HANDLES_PATH))
            if isinstance(data, list):
                return {str(x).strip().lower().lstrip("@") for x in data if str(x).strip()}
    except Exception as e:
        print(f"[scout] blocked handles load failed: {e}")
    return set()



def load_keep_handles():
    try:
        if os.path.exists(KEEP_HANDLES_PATH):
            data = json.load(open(KEEP_HANDLES_PATH))
            if isinstance(data, list):
                return {str(x).strip().lower().lstrip("@") for x in data if str(x).strip()}
    except Exception as e:
        print(f"[scout] keep handles load failed: {e}")
    return set()

def parse_date(date_str):
    if not date_str:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S %Y"):
        try:
            dt = datetime.datetime.strptime(date_str, fmt)
            return dt.date().isoformat()
        except Exception:
            continue
    return None


def has_recent_crypto_tweet_signal(screen_name, keywords):
    global TWEET_SIGNAL_LOOKUP_COUNT, SOFT_RATE_LIMIT_MODE, TWEET_SIGNAL_DISABLED
    key = (screen_name or "").strip()
    if not key:
        return False
    if key in TWEET_SIGNAL_CACHE:
        return TWEET_SIGNAL_CACHE[key]
    if SOFT_RATE_LIMIT_MODE or TWEET_SIGNAL_DISABLED:
        TWEET_SIGNAL_CACHE[key] = False
        return False
    if TWEET_SIGNAL_LOOKUP_COUNT >= MAX_TWEET_SIGNAL_LOOKUPS:
        TWEET_SIGNAL_DISABLED = True
        print(f"[checkpoint] tweet_signal_circuit_breaker | lookups={TWEET_SIGNAL_LOOKUP_COUNT}")
        TWEET_SIGNAL_CACHE[key] = False
        return False
    TWEET_SIGNAL_LOOKUP_COUNT += 1
    res = run_xreach(["tweets", key, "-n", "1", "--json"])
    if not isinstance(res, dict):
        TWEET_SIGNAL_CACHE[key] = False
        return False
    items = res.get("items", []) or []
    hard_crypto_markers = {
        "onchain", "defi", "tempo", "tao", "bittensor", "subnet",
        "inscription", "token", "wallet", "prediction", "perps", "launchpad", "zk", "l2", "awp", "predict", "mining",
        "bridge", "dex", "amm", "staking", "liquidity", "mainnet", "testnet", "evm", "solidity",
        "yield", "airdrop", "mint", "swap", "vault", "restaking", "rollup", "polymarket"
    }
    weak_or_ambiguous_terms = {
        "ai", "agent", "agentic", "builder", "building", "research", "researcher",
        "automation", "tool", "tools", "platform", "infra", "market"
    }
    negative_generic_markers = {
        "mindset", "motivation", "discipline", "personal growth", "self improvement",
        "product hunt", "build in public", "startup", "saas", "wellness", "massage", "spa",
        "clinic", "salon", "beauty", "therapy", "therapist", "healing", "coach", "coaching",
        "tutorial", "how to", "life", "lifestyle", "sports", "gamer", "preparation is",
        "actorrise", "scenepartner", "daily mindset", "growth mindset"
    }
    for item in items:
        text = (item.get("fullText") or item.get("text") or "").lower()
        hard_hits = sum(1 for k in hard_crypto_markers if k in text)
        weak_hits = sum(1 for k in weak_or_ambiguous_terms if k in text)
        if any(bad in text for bad in negative_generic_markers) and hard_hits == 0:
            continue
        if hard_hits >= 1:
            TWEET_SIGNAL_CACHE[key] = True
            return True
        if hard_hits == 0 and weak_hits >= 2:
            continue
    TWEET_SIGNAL_CACHE[key] = False
    return False


def extract_wallets(text):
    found = re.findall(r"0x[a-fA-F0-9]{40}", text or "")
    return ", ".join(sorted(set(found))) if found else ""


def notion_create_page(token, payload):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, method="POST", headers=headers, data=json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as f:
            return json.loads(f.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        print(f"[scout] Notion push failed: HTTP {e.code} {e.reason} :: {body[:500]}")
        return None
    except Exception as e:
        print(f"[scout] Notion push failed: {e}")
        return None


def notion_patch_page(page_id, token, props):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    payload = {"properties": props}
    req = urllib.request.Request(url, method="PATCH", headers=headers, data=json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as f:
            resp = json.loads(f.read().decode())
            return resp.get("object") == "page"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        print(f"[scout] Notion patch failed: HTTP {e.code} {e.reason} :: {body[:500]}")
        return False
    except Exception as e:
        print(f"[scout] Notion patch failed: {e}")
        return False


def notion_find_existing_page(token, dbid, handle_title, handle_url):
    candidates = []
    base = (handle_title or "").strip()
    if base:
        candidates.append(base)
        clean = base[1:] if base.startswith("@") else base
        candidates.append(clean)
        candidates.append(f"@{clean.lower()}")
        candidates.append(clean.lower())
    urls = []
    if handle_url:
        urls.append(handle_url)
        clean = (handle_url.rstrip("/").split("/")[-1] or "").strip()
        if clean:
            urls.extend([f"https://x.com/{clean}", f"https://twitter.com/{clean}"])
    or_filters = []
    seen_filter = set()
    for cand in candidates:
        key = ("handle", cand)
        if cand and key not in seen_filter:
            seen_filter.add(key)
            or_filters.append({"property": "Handle", "title": {"equals": cand}})
    for url in urls:
        key = ("url", url)
        if url and key not in seen_filter:
            seen_filter.add(key)
            or_filters.append({"property": "X Link", "url": {"equals": url}})
    query = {
        "page_size": 10,
        "filter": {"or": or_filters[:20]} if len(or_filters) > 1 else (or_filters[0] if or_filters else {})
    }
    try:
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{dbid}/query",
            data=json.dumps(query).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as f:
            resp = json.loads(f.read().decode())
            results = resp.get("results", [])
            return results[0] if results else None
    except Exception as e:
        print(f"[scout] notion lookup failed for {handle_title}: {e}")
        return None


def categorize_account(bio, handle=""):
    """Categorize account into sector based on bio keywords (matching Kevin's web categories)."""
    text = f"{handle} {bio}".lower()
    categories = {
        "AI": ["ai agent", "ai-powered", "artificial intelligence", "machine learning", "llm", "autonomous agent", "inference", "neural", "gpt", "claude", "reasoning", "agentic", "ai native"],
        "DeFi": ["defi", "dex", "amm", "swap", "lending", "borrowing", "yield", "liquidity", "vault", "perps", "derivatives", "options", "staking", "prediction market"],
        "NFT": ["nft", "collection", "inscription", "ordinal", "mint", "pfp", "art on chain", "digital art"],
        "GameFi": ["gamefi", "game on", "play to earn", "p2e", "onchain game", "on-chain game", "arcade", "gaming"],
        "DePIN": ["depin", "compute", "gpu", "bandwidth", "storage", "iot", "physical infrastructure"],
        "Layer-1 Blockchains": ["layer 1", "l1", "blockchain", "mainnet", "consensus", "validator", "node operator"],
        "Layer-2 Scaling Solutions": ["layer 2", "l2", "rollup", "zk", "optimistic", "scaling"],
        "Privacy": ["privacy", "zero knowledge", "zk proof", "anonymous", "confidential", "encrypted"],
        "Meme Coins": ["meme", "memecoin", "doge", "pepe", "shib", "pump"],
        "Stablecoins": ["stablecoin", "usd", "usdt", "usdc", "pegged"],
        "RWA": ["rwa", "real world asset", "tokenized", "real estate on chain"],
    }
    best_cat = "Unclear"
    best_score = 0
    for cat, terms in categories.items():
        score = sum(1 for t in terms if t in text)
        if score > best_score:
            best_score = score
            best_cat = cat
    return best_cat if best_score >= 1 else "Unclear"


def get_follower_tier(followers):
    """Assign follower tier label."""
    if followers == 0:
        return "0 Followers"
    elif followers <= 50:
        return "Under 50"
    elif followers <= 100:
        return "Under 100"
    elif followers <= 250:
        return "Under 250"
    elif followers <= 500:
        return "Under 500"
    else:
        return "500+"


def push_to_byfollower_notion(handle, bio, followers, source_seed="", smart_followers=0, quality_score=0, alpha_tier="", account_created=None):
    """Push accepted account to the 'X Sniper By Follower' Notion DB."""
    try:
        nv = load_env(BYFOLLOWER_NOTION_ENV)
        token = nv["NOTION_TOKEN"]
        dbid = nv["NOTION_DATABASE_ID"]
    except Exception as e:
        print(f"[scout] byfollower notion env missing: {e}")
        return False

    handle_title = f"@{handle}" if not handle.startswith("@") else handle
    handle_url = f"https://x.com/{handle.lstrip('@')}"
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    category = categorize_account(bio, handle)
    tier = get_follower_tier(followers)

    # Check if already exists
    existing = notion_find_existing_page(token, dbid, handle_title, handle_url)
    if existing:
        notion_patch_page(existing.get("id"), token, {
            "Last Checked": {"date": {"start": now}},
            "Followers": {"number": int(followers)},
            "Tier": {"select": {"name": tier}},
        })
        return True

    props = {
        "Handle": {"title": [{"text": {"content": handle_title}}]},
        "Bio": {"rich_text": [{"text": {"content": (bio or "")[:1800]}}]},
        "Followers": {"number": int(followers)},
        "Tier": {"select": {"name": tier}},
        "Alpha Tier": {"select": {"name": category}},
        "Date Added": {"date": {"start": now}},
        "Last Checked": {"date": {"start": now}},
        "Source Seed": {"rich_text": [{"text": {"content": (source_seed or "")[:500]}}]},
    }
    if smart_followers:
        props["Smart Followers Count"] = {"number": int(smart_followers)}
    if quality_score:
        props["Quality Score"] = {"number": int(quality_score)}
    if account_created:
        props["Account Created"] = {"date": {"start": account_created}}

    payload = {"parent": {"database_id": dbid}, "properties": props}
    resp = notion_create_page(token, payload)
    if resp:
        print(f"[checkpoint] byfollower_notion_created | {handle_title} | tier={tier} | cat={category}")
        return True
    return False


def load_reject_notion_env():
    try:
        if REJECT_NOTION_SOURCE.endswith('.env'):
            env_vars = {}
            for line in open(REJECT_NOTION_SOURCE):
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    env_vars[k.strip()] = v.strip()
            token = env_vars.get('NOTION_TOKEN', '')
            dbid = env_vars.get('NOTION_DATABASE_ID', '')
            if token and dbid:
                return {"NOTION_TOKEN": token, "NOTION_DATABASE_ID": dbid}
        # fallback: old text-file format
        lines = [line.strip() for line in open(REJECT_NOTION_SOURCE) if line.strip()]
        token = next((line for line in lines if line.startswith("ntn_")), None)
        dbid = None
        for line in lines:
            m = re.search(r'notion\.so/([0-9a-f]{32})', line)
            if m:
                dbid = m.group(1)
                break
        if token and dbid:
            return {"NOTION_TOKEN": token, "NOTION_DATABASE_ID": dbid}
    except Exception as e:
        print(f"[scout] reject Notion env load failed: {e}")
    return None


def ensure_notion_date_property_for_env(env_path, name):
    try:
        nv = load_env(env_path)
        token = nv["NOTION_TOKEN"]
        dbid = nv["NOTION_DATABASE_ID"]
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{dbid}",
            data=json.dumps({"properties": {name: {"date": {}}}}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=30) as f:
            json.loads(f.read().decode())
        return True
    except Exception as e:
        print(f"[scout] ensure date property failed for {name}: {e}")
        return False


def push_rejects_to_notion(records, deadline_ts=None):
    env = load_reject_notion_env()
    if not env or not records:
        return 0, 0
    token = env["NOTION_TOKEN"]
    dbid = env["NOTION_DATABASE_ID"]
    now_dt = datetime.datetime.now(datetime.UTC)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    snapshot_mode = os.environ.get("ALPHA_REJECT_SNAPSHOT_MODE", "0").lower() not in {"0", "false", "off", "no"}
    ensure_notion_date_property_for_env(REJECT_NOTION_SOURCE, "Account Created")
    created = 0
    updated = 0
    seen = set()
    skip_reasons = {"missing_followers", "known_existing", "main_notion_follower_cap"}
    max_push = int(os.environ.get("ALPHA_REJECT_PUSH_MAX", "120"))
    pushed = 0
    for rec in records:
        if deadline_ts and time.monotonic() >= deadline_ts:
            print(f"[checkpoint] reject_push_deadline_stop | pushed={pushed} | created={created} | updated={updated}")
            break
        if pushed >= max_push:
            print(f"[checkpoint] reject_push_cap_stop | max={max_push} | created={created} | updated={updated}")
            break
        reason = (rec.get("reason") or "").strip()
        if reason in skip_reasons:
            continue
        if detect_east_asia_lane(rec.get('source') or '', rec.get('bio') or '') in {'china', 'japan'}:
            continue
        handle = (rec.get("handle") or "").strip()
        if not handle:
            continue
        clean = handle[1:] if handle.startswith("@") else handle
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        base_handle_title = f"@{clean}"
        handle_title = base_handle_title
        handle_url = f"https://x.com/{clean}"
        existing = None if snapshot_mode else notion_find_existing_page(token, dbid, base_handle_title, handle_url)
        props = {
            "Handle": {"title": [{"text": {"content": handle_title}}]},
            "X Link": {"url": handle_url},
            "Followers": {"number": int(rec.get("followers") or 0)},
            "Bio": {"rich_text": [{"text": {"content": str((rec.get("bio") or "")[:1800])}}]},
            "Reason": {"rich_text": [{"text": {"content": str(reason[:500])}}]},
            "Source": {"rich_text": [{"text": {"content": str(((rec.get("source") or rec.get("source_seed") or "")[:500]))}}]},
            "Recent Username": {"rich_text": [{"text": {"content": base_handle_title}}]},
        }
        account_created = rec.get("account_created") or rec.get("created") or rec.get("created_at")
        if account_created:
            props["Account Created"] = {"date": {"start": str(account_created)}}
        if existing:
            # Existing rejects are historical state, not fresh discoveries.
            # Do not touch Last Added or manual review/check properties; only refresh machine fields.
            if notion_patch_page(existing.get("id"), token, props):
                updated += 1
            pushed += 1
            continue
        props["Last Added"] = {"date": {"start": now}}
        payload = {"parent": {"database_id": dbid}, "properties": props}
        if notion_create_page(token, payload):
            created += 1
        pushed += 1
        time.sleep(0.08)
    return created, updated


def is_china_source(source_seed):
    s = (source_seed or '').lower()
    if 'china:@' in s:
        return True
    china_markers = [
        'following:@s26620895', 'following:@cryptodajiba', 'following:@giggle_kingdom',
        'following:@mfpurrs_eths', 'following:@0x_fuhei', 'following:@0x17479',
        'following:@chichi_ailmy', 'following:@skye1297049849', 'following:@amanjollll',
        'following:@cyptoccc', 'following:@like31638680',
        # 2026-05-22 batch: Kevin-provided China seeds (random follow tendency, project-only gate)
        'following:@juesaiquan', 'following:@yuui_732', 'following:@deghnl6',
        'following:@crypto_boy1520', 'following:@real_dr_pump', 'following:@ordiboysxwg',
        'following:@xconanresearch', 'following:@feng_btc', 'following:@dongmoom55',
        'following:@xinchne_eth', 'following:@yangxiao214', 'following:@0xjarr',
        'following:@luyuban69', 'following:@big_tea_rice', 'following:@mrwick8848',
        'following:@tiger_web3', 'following:@flai6666', 'following:@reboottttttt',
        'following:@gamefimaster', 'following:@brc20_gd', 'following:@u9_blockchain',
        'following:@spark888', 'following:@0xlinx349', 'following:@fc2068',
        'following:@unipioneer', 'following:@kiki25140290', 'following:@01573bit',
        'following:@spicycandy00', 'following:@aiqiang888', 'following:@0x9825',
        'following:@xeogy92298766', 'following:@congge918'
    ]
    return any(m in s for m in china_markers)




def strong_builder_signal(text):
    s = (text or '').lower()
    return any(t in s for t in [
        'shipped', 'deployed', 'pushed to mainnet', 'open sourced', 'repo',
        'contract address', 'docs live', 'sdk released', 'api live',
        'dashboard live', 'building', 'shipping', 'hacking on',
        'early prototype', 'testing idea', 'beta live', 'playground',
        'devnet', 'mainnet', 'primitive', 'experiment'
    ])

def pre_accept_quality_score(profile_blob, source_seed=""):
    """Precision-first pre-gate for main Notion. Returns (score, reasons).
    This is intentionally stricter than broad crypto detection: main DB should contain useful alpha, not random crypto.
    """
    text = (profile_blob or "").lower()
    source = (source_seed or "").lower()
    score = 0
    reasons = []

    thesis_terms = [
        "arc", "tempo", "x402", "mpp", "tao", "bittensor", "subnet", "opentensor",
        "awp", "predict", "prediction market", "polymarket", "onchain ai", "ai agent",
        "agentic", "decentralized ai", "zkml", "ai inference", "quantum", "inscription",
        "ordinal", "runes", "primitive", "genesis", "devnet", "mainnet soon", "on-chain", "free mint", "freemint",
        "depin", "ai mining", "ai earn", "nft marketplace", "launchpad", "agent marketplace",
        "mcp", "worknet", "compute", "gpu", "inference", "data network", "ai network",
        "ai protocol", "autonomous", "on-chain ai", "onchain agent", "ai finance",
        "ai trading", "ai yield", "ai vault", "restaking", "liquid staking",
        "perps", "perpetual", "options", "derivatives", "clearing", "settlement layer",
        "intent", "solver", "mev", "block builder", "sequencer", "da layer",
        "social fi", "socialfi", "farcaster", "lens", "friend tech",
        "gaming", "gamefi", "play to earn", "move to earn", "real world asset", "rwa",
        "stablecoin", "cdp", "lending", "borrowing", "yield aggregator",
        "cross-chain", "bridge", "interop", "modular", "rollup as a service",
        "zk proof", "validity proof", "fraud proof",
        "security", "audit", "monitoring", "guard", "firewall",
        "payment", "transfer", "remittance", "payroll",
        "marketplace", "exchange", "aggregator", "router",
        "dao tooling", "governance", "treasury", "multisig",
        "identity", "did", "credential", "attestation", "soulbound",
        "storage", "ipfs", "arweave", "filecoin", "data availability",
        "trading bot", "sniper bot", "mev bot",
        "onchain analytics", "blockchain explorer", "chain scanner", "defi dashboard",
        "nft collection", "pfp", "generative nft", "mint page",
        "gamefi", "play to earn", "move to earn", "onchain game",
        "socialfi", "social fi", "farcaster", "lens protocol",
        "rollup", "appchain", "l2", "l3", "da layer",
        "oracle", "price feed", "data feed", "indexer", "subgraph",
        "launchpad", "token launch", "fair launch",
        "web3 accelerator", "crypto incubator", "web3 hackathon"
    ]
    builder_terms = [
        "building", "built on", "deployed", "shipped", "docs", "repo", "sdk", "api", "mcp",
        "compiler", "runtime", "protocol", "infra", "infrastructure", "payments", "settlement",
        "wallet", "validator", "miner", "subnet", "experiment", "prototype", "beta", "devnet",
        "mainnet", "first", "native", "launch", "launching", "live", "open source",
        "smart contract", "dapp", "onchain", "decentralized", "permissionless",
        "non-custodial", "trustless", "composable", "modular", "interoperable"
    ]
    early_terms = ["first", "early", "genesis", "initial", "v0", "beta", "experimental", "primitive", "phase 0", "native", "devnet", "mainnet soon", "live soon"]
    hard_bad = [
        "airdrop", "giveaway", "whitelist", "wl spots", "free mint", "mint now", "join discord", "invite contest", "comment done", "like and retweet", "drop your wallet",
        "farming", "grind", "daily task", "testnet reward", "retroactive", "alpha thread", "raid", "alpha caller", "airdrop hunter", "testnet hunter",
        "kol", "influencer", "marketing", "growth lead", "ambassador", "community manager",
        "partnerships", " bd ", "bd ", "nft promoter", "web3 marketer", "collab manager"
    ]
    soft_bad = ["community", "vibes", "gm", "wagmi", "lfg", "moon", "ape", "degen", "nfa", "dyor", "trader", "collector", "researcher"]
    broad_only = ["base", "solana", "ethereum", "bitcoin", "btc", "eth", "wallet", "token", "dex", "defi", "nft", "web3", "crypto"]

    thesis_hits = sum(1 for t in thesis_terms if t in text)
    builder_hits = sum(1 for t in builder_terms if t in text)
    early_hits = sum(1 for t in early_terms if t in text)
    hard_bad_hits = sum(1 for t in hard_bad if t in text)
    soft_bad_hits = sum(1 for t in soft_bad if t in text)
    broad_hits = sum(1 for t in broad_only if t in text)
    thesis_source = any(tag in source for tag in ["search:arc_base:", "search:tempo:", "search:tao_subnet:", "search:ai_general:", "retry:arc_base:", "retry:tempo:", "retry:tao_subnet:", "retry:ai_general:", "following:@awp_protocol", "following:@chronosontempo", "following:@tempoagent"])

    if thesis_hits:
        score += min(5, thesis_hits * 2); reasons.append(f"thesis={thesis_hits}")
    if builder_hits:
        score += min(4, builder_hits); reasons.append(f"builder={builder_hits}")
    if early_hits:
        score += min(3, early_hits); reasons.append(f"early={early_hits}")
    # Chain name mentions = weak crypto signal (+1, not full thesis)
    chain_names = ["solana", "ethereum", "base", "polygon", "arbitrum", "optimism", "avalanche", "sui", "aptos", "near", "cosmos", "polkadot", "ton", "sei", "monad", "berachain"]
    chain_hits = sum(1 for cn in chain_names if cn in text)
    if chain_hits and not thesis_hits:
        score += 1; reasons.append(f"chain_name={chain_hits}")
    # Source from known crypto seed = weak signal, but only if bio has at least one broad crypto term
    if source.startswith("following:@") and not thesis_hits and not chain_hits and broad_hits >= 1:
        score += 1; reasons.append("crypto_seed_source")
    urgent_seed_source = any(tag in source for tag in [
        'following:@0xdetweiler', 'following:@0xleonidas', 'following:@0xwhacko', 'following:@aezchain',
        'following:@apeinafarm', 'following:@bearzverse', 'following:@christxbt', 'following:@crypto_goatinho',
        'following:@cryptogoatinho', 'following:@hbh_zhr', 'following:@honka_yo', 'following:@honkayo',
        'following:@ibraxbt', 'following:@impossiblemach', 'following:@kido201196', 'following:@mrrose',
        'following:@mutu_1987', 'following:@rightpitch20', 'following:@sonofd0nda', 'following:@unsynned',
        'following:@web3gus', 'following:@xweth', 'following:@yangxiao214',
        'following:@nonfungiblemomo', 'following:@nickplayscrypto', 'following:@office2crypto', 'following:@moitreghason', 'following:@amplifi_now', 'following:@td_0101', 'following:@3dmax_virtuals', 'following:@gkisokay', 'following:@bigwil', 'following:@goon_crypto', 'following:@100xdarren', 'following:@idaratbn', 'following:@biig_classic1', 'following:@qinglong0791', 'following:@siyabaldacc', 'following:@skywalker_t25', 'following:@0xalphanur', 'following:@richard_ola_', 'following:@btcmonie', 'following:@nikitont', 'following:@xiaobi988', 'following:@emilylweb3', 'following:@boo_man69', 'following:@trathoa', 'following:@b4kenny', 'following:@0itsali0', 'following:@m0ment0_', 'following:@arow299', 'following:@dazzlercoin', 'following:@tenacious_ar', 'following:@notab2d_', 'following:@afrectz', 'following:@tma_420', 'following:@canducrypto', 'following:@sulu0x', 'following:@xiaohao67942591', 'following:@kimmykim_nft', 'following:@santiagoroel', 'following:@mayamaster', 'following:@thacryptoninja', 'following:@pepedrt', 'following:@0xlaughing', 'following:@iamheci', 'following:@0xgeeky'
    ])
    if urgent_seed_source and broad_hits >= 1 and hard_bad_hits == 0:
        score += 2; reasons.append('urgent_seed_source')
    if urgent_seed_source and thesis_hits >= 1:
        score += 2; reasons.append('urgent_seed_thesis')
    if thesis_source:
        score += 2; reasons.append("thesis_source")
    if broad_hits >= 4 and not thesis_hits:
        score -= 1; reasons.append(f"broad_only=-1")
    if hard_bad_hits:
        score -= 6; reasons.append(f"hard_bad={hard_bad_hits}")
    if soft_bad_hits and thesis_hits == 0:
        score -= min(3, soft_bad_hits); reasons.append(f"soft_bad_no_thesis={soft_bad_hits}")
    # Recent-cleanup calibration: block personal/profile bios that slipped in via
    # broad ARC/search terms or generic following graph. Main Notion should be
    # project/product/protocol surfaces, not reporters, founders, researchers, or CT users.
    recent_personal_noise = [
        "proud intp", "cat mom", "former tech reporter", "based bitcoiner",
        "testing products", "staying early", "if it brings value", "connect if you're building",
        "#buildinpublic", "buildinpublic", "hodl #eth", "hodl #sui",
        "fixing broken distribution systems", "quantitative strategist", "security researcher",
        "founder & ceo", "founder at http", "web3 participant", "investment researcher",
        "sharing people and things", "project creator and investment", "builder at @", "dev: @"
    ]
    if any(p in text for p in recent_personal_noise):
        score -= 8; reasons.append("recent_personal_noise")
    if individual_noise_score(text) >= 1 and projectish_signal_score(text) < 3:
        score -= 4; reasons.append("individual_not_project")
    if len(text.strip()) < 24:
        # Don't penalize thin bios if handle looks project-like
        handle_part = text.split()[0] if text.split() else ""
        project_suffixes = ["xyz", "fi", "dao", "labs", "protocol", "network", "chain"]
        if any(handle_part.endswith(s) for s in project_suffixes):
            score += 1; reasons.append("project_handle")
        else:
            score -= 1; reasons.append("thin_bio")

    return score, reasons

def bio_language_allowed(text):
    """Allow English/ASCII plus Chinese/Mandarin/CJK and Japanese scripts. Block other dominant non-English scripts.
    Unicode-styled Latin text is normalized first, so fancy English fonts still count as English.
    """
    s = unicodedata.normalize("NFKD", text or "")
    letters = [ch for ch in s if ch.isalpha()]
    if not letters:
        return True
    ascii_letters = sum(1 for ch in letters if ord(ch) < 128)
    cjk = sum(1 for ch in letters if 0x4E00 <= ord(ch) <= 0x9FFF)
    hira = sum(1 for ch in letters if 0x3040 <= ord(ch) <= 0x309F)
    kata = sum(1 for ch in letters if 0x30A0 <= ord(ch) <= 0x30FF)
    allowed = ascii_letters + cjk + hira + kata
    other = len(letters) - allowed
    if other >= 3 and other > allowed:
        return False
    if other >= 6 and allowed < 6:
        return False
    return True

def detect_east_asia_lane(source_seed, bio_text=''):
    s = (source_seed or '').lower()
    text = bio_text or ''
    b = text.lower()
    if is_china_source(source_seed):
        return 'china'
    has_hiragana = any(0x3040 <= ord(ch) <= 0x309F for ch in text)
    has_katakana = any(0x30A0 <= ord(ch) <= 0x30FF for ch in text)
    has_cjk = any(0x4E00 <= ord(ch) <= 0x9FFF for ch in text)
    japan_markers = [
        'japan:@', 'jp:@', 'following:@japan', 'following:@jp_', 'following:@_jp',
        'tokyo', 'osaka', 'kyoto', '日本', 'jp nft', 'japanese'
    ]
    if has_hiragana or has_katakana or any(m in s for m in japan_markers) or any(m in b for m in ['日本', '東京', '大阪', '京都', 'japanese']):
        return 'japan'
    if has_cjk:
        return 'china'
    return 'global'


def push_to_notion(handle_url, bio, account_created, followers, smart_follower, source_seed, wallets):
    cand_gate = {
        "sn": handle_url.rstrip('/').split('/')[-1],
        "bio": bio,
        "followers": followers,
        "source_seed": source_seed,
    }
    if not is_main_notion_candidate(cand_gate):
        print(f"[checkpoint] push_to_notion_gate_reject | @{cand_gate['sn']} | source={source_seed}")
        return None, None, False
    target_env = NOTION_ENV
    try:
        nv = load_env(target_env)
        token = nv["NOTION_TOKEN"]
        dbid = nv["NOTION_DATABASE_ID"]
    except Exception as e:
        print(f"[scout] missing Notion env: {e}")
        return None, None, False

    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    bio_safe = (bio or "")[:1800]

    handle_name = handle_url.rstrip("/").split("/")[-1]
    handle_title = f"@{handle_name}" if handle_name else handle_url

    existing = notion_find_existing_page(token, dbid, handle_title, handle_url)
    if existing:
        page_id = existing.get("id")
        updated = notion_patch_page(page_id, token, {
            "Last Checked": {"date": {"start": now}},
            "Followers": {"number": int(followers)},
            "X Link": {"url": handle_url},
        })
        if updated:
            print(f"[checkpoint] duplicate_skip | {handle_title} already exists in Notion")
            return page_id, existing.get("url"), False
        return None, None, False

    props = {
        "Handle": {"title": [{"text": {"content": handle_title}}]},
        "X Link": {"url": handle_url},
        "Bio": {"rich_text": [{"text": {"content": bio_safe}}]},
        "Followers": {"number": int(followers)},
        "Username History": {"rich_text": [{"text": {"content": "username: zero"}}]},
        "Date Added": {"date": {"start": now}},
        "Last Checked": {"date": {"start": now}},
    }
    if account_created:
        props["Account Created"] = {"date": {"start": account_created}}
    if source_seed:
        props["Source Seed"] = {"rich_text": [{"text": {"content": source_seed[:500]}}]}
    if wallets:
        props["Wallets"] = {"rich_text": [{"text": {"content": wallets[:1500]}}]}

    payload = {"parent": {"database_id": dbid}, "properties": props}
    resp = notion_create_page(token, payload)
    if not resp:
        return None, None, False
    # Also push to "By Follower" Notion DB for cleaner categorized view
    try:
        push_to_byfollower_notion(
            handle=handle_name, bio=bio, followers=int(followers),
            source_seed=source_seed,
            smart_followers=int(cand_gate.get("smart_followers") or 0),
            account_created=account_created,
        )
    except Exception as e:
        print(f"[scout] byfollower push error: {e}")
    return resp.get("id"), resp.get("url"), True


def run_frontrun(handle):
    cmd = FRONTRUN_CMD + [handle, "--json"]
    try:
        out = subprocess.check_output(cmd, cwd=FRONTRUN_DIR, text=True, stderr=subprocess.DEVNULL, timeout=45)
        return json.loads(out)
    except subprocess.CalledProcessError as e:
        print(f"[enrich] frontrun error for {handle}: {e}")
        return None
    except Exception as e:
        print(f"[enrich] exception for {handle}: {e}")
        return None


def load_frontrun_cache():
    try:
        if os.path.exists(FRONTRUN_CACHE_PATH):
            with open(FRONTRUN_CACHE_PATH) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[enrich] frontrun cache load failed: {e}")
    return {}


def save_frontrun_cache(cache):
    try:
        os.makedirs(os.path.dirname(FRONTRUN_CACHE_PATH), exist_ok=True)
        tmp = FRONTRUN_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FRONTRUN_CACHE_PATH)
    except Exception as e:
        print(f"[enrich] frontrun cache save failed: {e}")


def run_frontrun_cached(handle, max_age_hours=72, allow_live=True):
    global PREFLIGHT_FRONTRUN_COUNT
    key = str(handle or "").strip().lower().lstrip("@")
    if not key:
        return None, False
    cache = load_frontrun_cache()
    rec = cache.get(key)
    now = datetime.datetime.now(datetime.UTC)
    if isinstance(rec, dict) and isinstance(rec.get("data"), dict):
        try:
            ts = datetime.datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
            if (now - ts).total_seconds() <= max_age_hours * 3600:
                return {"ok": True, "data": rec.get("data")}, True
        except Exception:
            pass
    if not allow_live or PREFLIGHT_FRONTRUN_COUNT >= MAX_PREFLIGHT_FRONTRUN_PER_RUN:
        return None, False
    PREFLIGHT_FRONTRUN_COUNT += 1
    fr = run_frontrun(key)
    if fr and fr.get("ok") and isinstance(fr.get("data"), dict):
        cache[key] = {"ts": now.isoformat(), "data": fr.get("data")}
        save_frontrun_cache(cache)
    return fr, False


def extract_smart_followers(fr_data):
    try:
        if isinstance(fr_data, dict) and isinstance(fr_data.get("smartFollowers"), dict):
            return int(fr_data["smartFollowers"].get("totalCount") or 0)
    except Exception:
        pass
    return 0


def projectish_signal_score(text_blob):
    text = (text_blob or "").lower()
    project_terms = [
        "protocol", "app", "platform", "wallet", "launchpad", "marketplace", "dex", "amm",
        "infra", "sdk", "api", "docs", "mainnet", "devnet", "testnet", "network", "chain",
        "l1", "l2", "rollup", "bridge", "payments", "settlement", "prediction market",
        "tooling", "built on", "powered by", "launching on", "live on", "launched on",
        "subnet", "inscription", "agent", "agentic", "miner", "validator", "runtime", "compiler",
        "collection", "nft collection", "casino", "arena", "game on", "on @tempo", "on @arc",
        "on @base", "on @solana", "freemint", "free mint", "supply", "pieces on",
        "building on", "building a", "we build", "we're building", "first on", "first to", "powering the", "enabling", "automating",
        "exchange", "aggregator", "router", "lending", "borrowing", "credit", "yield", "vault",
        "options", "perps", "derivatives", "privacy", "identity", "index", "rpc", "node operators",
        "security", "pentesting", "on-chain game", "onchain game", "gamefi", "launch a coin",
        "portfolio tracker", "onchain recovery", "passkeys", "self-custody", "defi agents"
    ]
    return sum(1 for t in project_terms if t in text)


def individual_noise_score(text_blob):
    text = (text_blob or "").lower()
    individual_terms = [
        "trader", "daytrader", "investor", "enthusiast", "shitposting", "daily alpha",
        "early gems", "gem hunter", "degen", "designer", "artist", "analyst",
        "follow for", "dms open", "threads", "content creator", "writer", "kol", "influencer",
        "personal account", "head of bd", " bd ", "maximalist", "professional anon", "community manager",
        "ambassador", "growth", "marketing", "partnerships", "founder of", "co-founder",
        "exploring", "learning", "journey", "dream", "believer", "lover", "passionate about",
        "crypto since", "in crypto since", "opinions are my own", "not financial advice",
        "views are my own", "just a guy", "just vibing", "here for the",
        "testnet user", "beginner", "financial independence", "grow together", "building toward",
        "sharp reads", "crypto markets", "daily updates", "follow back",
        "follow for follow", "subscribe", "lets grow", "let's grow",
        # Additional individual/noise patterns (2026-05-06 cleanup)
        "less talk", "more ship", "ideas \u00b7 systems", "sweet, cheerful", "energetic, fun",
        "web3 content", "web3 girlie", "onchain normie", "playing with magic internet money",
        "i share my thoughts", "it's my live", "play stupid games", "still not rich",
        "day 1000 on ct", "lost money on", "youtuber", "blind js", "php dev in web3",
        "software engineer", "dev in web3", "reviews of web3", "base ambassador",
        "managing @coinbase", "finance @", "comm", "community",
        "opinions", "my thoughts", "share thoughts", "build income",
        "\u6700\u8fd1\u306f", "\u666e\u901a\u73a9\u5bb6", "\u505a\u591a\u65f6\u95f4", "\u505a\u7a7a\u566a\u97f3",
        "持有btc", "全力梭哈", "trust @tempo",
        # 2026-05-20: patterns from Kevin's web analysis (clear vs unclear separation)
        "educator", "professor", "teacher", "coach", "mentor",
        "daily feed", "your daily", "daily dose", "daily thread",
        "news aggregator", "notícias", "noticias", "newsletter",
        "msc ", "phd ", "mba ", "graduate",
        "just a ", "just chil", "just here", "just some",
        "i am a", "i'm a", "i help ", "i write", "i teach",
        "el rincón", "en español", "em português",
        "market news", "market insight", "macro insight",
        "cloud engineer", "data scientist", "ml engineer",
        "freelance", "consultant", "advisor", "strategist",
        "dad ", "mom ", "husband", "wife ", "human ",
        "student", "intern ", "aspiring",
        "ape in", "aping", "hodler", "hodling",
        "alpha caller", "alpha hunter", "gem caller",
        "deciphering", "demystifying", "explaining", "simplifying",
        "obsessing over", "curious about", "fascinated by",
        "god's favorite", "blessed", "grateful",
        "locked in", "grinding", "on my grind",
    ]
    return sum(1 for t in individual_terms if t in text)


def playable_project_thesis_score(handle, bio, source_seed=""):
    """Kevin-calibrated production gate for playable thesis surfaces.

    Prefer projects/apps/protocol surfaces users can play/use/monitor: AWP/worknet,
    Tempo/ARC NFT/inscription/launchpad/marketplace/x402 apps, prediction markets,
    AI earn/security arenas/agent finance. Avoid core/dev/founder/KOL/retail accounts.
    Returns (score, reasons).
    """
    text = " ".join([str(handle or ""), str(bio or ""), str(source_seed or "")]).lower()
    reasons = []
    score = 0
    playable_patterns = {
        "awp_worknet": ["awp", "worknet", "ai agents work", "agents work and earn", "mine worknet", "predict worknet"],
        "tempo_play": ["built on @tempo", "built on tempo", "on-chain pvp", "onchain pvp", "tempo reward", "tempo built-in reward", "powered by @mpp", "tempo nft", "tempo inscription", "tempo launchpad", "tempo marketplace", "tempo x402", "x402 on tempo", "tempo payment app", "tempo wallet"],
        "arc_play": ["building on @arc", "built on @arc", "building on arc", "built on arc", "trade on arc", "on @arc", "arc nft", "cat nfts on @arc", "arc inscription", "inscription on arc", "arc marketplace", "arc launchpad", "x402 arc", "x402 on arc", "arc payment app", "arc prediction market", "arc testnet"],
        "prediction_market": ["prediction market", "on-chain market", "onchain market", "singularity on-chain market", "prop firm", "event market", "narrative market", "resolve market"],
        "ai_earn": ["attack ai", "get paid", "ai security arena", "agentic finance", "one agent", "every market", "ai agent rewards", "agent rewards", "agent mining", "agent revenue", "agent work", "agent earns", "clawpump"],
        "web3_ai_protocol": ["automaton", "x402", "agent wallet", "real infrastructure", "live data feeds", "agent protocol", "agent marketplace", "decentralized inference", "verifiable inference", "ai compute marketplace"],
    }
    for lane, pats in playable_patterns.items():
        hits = [pat for pat in pats if pat in text]
        if hits:
            score += 4 + min(6, len(hits) * 2)
            reasons.append(f"{lane}:{','.join(hits[:3])}")
    project_terms = ["protocol", "app", "marketplace", "launchpad", "wallet", "nft", "inscription", "payment", "payments", "x402", "mpp", "testnet", "mainnet", "live", "built on", "powered by", "trade", "mine", "earn", "reward", "rewards"]
    project_hits = [t for t in project_terms if t in text]
    if project_hits:
        score += min(6, len(project_hits)); reasons.append(f"project={len(project_hits)}")
    bad_core = ["founder @", "founder of", "board member", "building & investing", "incentivizing intelligence", "research house", "developer advocate", "devrel", "ecosystem lead"]
    bad_retail = ["serving retail", "retail via x", "paid tool your agent needs", "content creator", "marketing agency", "kol", "trader", "daily analytics", "news, updates", "collabs:"]
    bad_hits = [b for b in bad_core + bad_retail if b in text]
    if bad_hits:
        score -= 10 + len(bad_hits) * 2; reasons.append(f"bad={','.join(bad_hits[:3])}")
    return score, reasons


def is_playable_project_candidate(cand):
    score, reasons = playable_project_thesis_score(cand.get("sn") or cand.get("handle"), cand.get("bio"), cand.get("source_seed") or cand.get("source"))
    cand["playable_score"] = score
    cand["playable_reasons"] = reasons[:5]
    return score >= 8


def preflight_frontrun_eligible(cand):
    text_blob = " ".join([str(cand.get("sn") or ""), str(cand.get("bio") or "")]).lower()
    source = str(cand.get("source_seed") or "").lower()
    try:
        followers = int(cand.get("followers") or 0)
    except Exception:
        followers = 0
    if followers > 500:
        return False
    if individual_noise_score(text_blob) >= 1 and projectish_signal_score(text_blob) == 0:
        return False
    thesisish = any(t in text_blob or t in source for t in [
        "arc", "tempo", "x402", "mpp", "tao", "bittensor", "subnet", "opentensor",
        "awp", "predict", "prediction", "polymarket", "onchain ai", "ai agent",
        "agentic", "decentralized ai", "inscription", "quantum", "wallet", "payments"
    ])
    return thesisish and (projectish_signal_score(text_blob) >= 1 or len((cand.get("bio") or "").strip()) < 20)


def smart_follower_override_allowed(cand, fr_data):
    text_blob = " ".join([str(cand.get("sn") or ""), str(cand.get("bio") or "")]).lower()
    source = str(cand.get("source_seed") or "").lower()
    followers = int(cand.get("followers") or 0)
    smart_followers = extract_smart_followers(fr_data)
    thesisish = any(t in text_blob or t in source for t in [
        "arc", "tempo", "x402", "mpp", "tao", "bittensor", "subnet", "opentensor",
        "awp", "predict", "prediction", "polymarket", "onchain ai", "ai agent",
        "agentic", "decentralized ai", "inscription", "quantum", "wallet", "payments"
    ])
    if followers > 1000 or smart_followers < 3 or not thesisish:
        return False
    # Smart followers can rescue thin/zero-post project accounts, not random individual/KOL accounts.
    if individual_noise_score(text_blob) >= 1 and projectish_signal_score(text_blob) == 0:
        return False
    return projectish_signal_score(text_blob) >= 1 or len((cand.get("bio") or "").strip()) < 12


def enrich_with_frontrun(page_id, token, fr_data):
    props = {}
    if isinstance(fr_data.get("ethos"), dict):
        score = fr_data["ethos"].get("score")
        if score is not None:
            props["Ethos Score"] = {"number": int(score)}
        level = fr_data["ethos"].get("level")
        if level:
            props["Ethos Level"] = {"rich_text": [{"text": {"content": str(level)[:500]}}]}

    if isinstance(fr_data.get("smartFollowers"), dict):
        total = fr_data["smartFollowers"].get("totalCount")
        if total is not None:
            props["Smart Followers Count"] = {"number": int(total)}

    if isinstance(fr_data.get("tweetsWithCA"), dict):
        undeleted = fr_data["tweetsWithCA"].get("undeletedCACount")
        deleted = fr_data["tweetsWithCA"].get("deletedCACount")
        if undeleted is not None:
            props["CA Undeleted"] = {"number": int(undeleted)}
        if deleted is not None:
            props["CA Deleted"] = {"number": int(deleted)}

    if isinstance(fr_data.get("account"), dict):
        wallets = fr_data["account"].get("wallets", [])
        addrs = []
        for w in wallets:
            addr = w.get("address") if isinstance(w, dict) else str(w)
            if addr:
                addrs.append(addr)
        if addrs:
            props["Linked Wallets"] = {"rich_text": [{"text": {"content": ", ".join(addrs)[:1500]}}]}

    # Username history from frontrun
    if isinstance(fr_data.get("usernameHistory"), dict):
        current = fr_data["usernameHistory"].get("currentTwitterUsername") or ""
        hist = fr_data["usernameHistory"].get("usernameHistory") or []
        hist_names = []
        for item in hist:
            if isinstance(item, str):
                hist_names.append(item)
            elif isinstance(item, dict):
                name = item.get("username") or item.get("screenName") or item.get("name")
                if name:
                    hist_names.append(str(name))
        if hist_names:
            txt = f"previous: {', '.join(hist_names)} | current: @{current}"
        else:
            txt = f"username: zero | current: @{current}" if current else "username: zero"
        props["Username History"] = {"rich_text": [{"text": {"content": txt[:1500]}}]}

    if not props:
        return False

    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    payload = {"properties": props}
    req = urllib.request.Request(url, method="PATCH", headers=headers, data=json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as f:
            resp = json.loads(f.read().decode())
            return resp.get("object") == "page"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        print(f"[enrich] Notion patch failed: HTTP {e.code} {e.reason} :: {body[:500]}")
        return False
    except Exception as e:
        print(f"[enrich] Notion patch failed: {e}")
        return False


def score_alpha_tier(bio, followers, ethos_score=None, smart_followers=0, ca_undeleted=0, ca_deleted=0):
    bio = (bio or "").lower()
    crypto_terms = ['crypto','web3','onchain','defi','nft','protocol','infra','wallet','dex','staking','bridge','rollup','zk','l2','evm','solidity','perps','prediction','payments','mainnet','testnet','liquidity','token','launchpad','trading','market','tempo','bittensor','tao','subnet','inscription','awp','predict','mining']
    builder_terms = ['build','building','builder','research','agent','agentic','ai','automation','scanner','tracker','tool','platform','layer 1','layer 2','aggregation','aggregator','miner','validator']
    vague_terms = ['community','vibes','soul','future','journey','dm for promo','collab manager','alpha caller','nfa','degen','memecoin','meme guy','shitposting']

    score = 0
    crypto_hits = sum(1 for t in crypto_terms if t in bio)
    builder_hits = sum(1 for t in builder_terms if t in bio)
    vague_hits = sum(1 for t in vague_terms if t in bio)

    if crypto_hits >= 2:
        score += 2
    elif crypto_hits == 1:
        score += 1
    if builder_hits >= 2:
        score += 2
    elif builder_hits == 1:
        score += 1
    if 10 <= followers <= 300:
        score += 2
    elif 1 <= followers < 10:
        score += 1
    elif 301 <= followers <= 500:
        score += 1
    if smart_followers >= 5:
        score += 2
    elif smart_followers >= 1:
        score += 1
    if ethos_score and ethos_score >= 1100:
        score += 1
    if ca_undeleted >= 3:
        score += 1
    if ca_deleted >= 3:
        score -= 1
    if vague_hits >= 1 and crypto_hits == 0:
        score -= 2
    elif vague_hits >= 2:
        score -= 1
    if len(bio.strip()) < 20:
        score -= 1

    if score >= 7:
        return 'A', score
    if score >= 4:
        return 'B', score
    return 'C', score


def ensure_notion_number_property(name):
    try:
        nv = load_env(NOTION_ENV)
        token = nv["NOTION_TOKEN"]
        dbid = nv["NOTION_DATABASE_ID"]
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{dbid}",
            data=json.dumps({"properties": {name: {"number": {"format": "number"}}}}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=30) as f:
            json.loads(f.read().decode())
        return True
    except Exception as e:
        print(f"[scout] ensure number property failed for {name}: {e}")
        return False


def ensure_notion_select_property_for_env(env_path, name, options):
    try:
        nv = load_env(env_path)
        token = nv["NOTION_TOKEN"]
        dbid = nv["NOTION_DATABASE_ID"]
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{dbid}",
            data=json.dumps({"properties": {name: {"select": {"options": options}}}}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=30) as f:
            json.loads(f.read().decode())
        return True
    except Exception as e:
        print(f"[scout] ensure select property failed for {name}: {e}")
        return False


def ensure_notion_select_property(name, options):
    return ensure_notion_select_property_for_env(NOTION_ENV, name, options)


def apply_quality_labels(page_id, bio, followers, fr_data=None, source_seed=""):
    ethos_score = None
    smart_followers = 0
    ca_undeleted = 0
    ca_deleted = 0
    if isinstance(fr_data, dict):
        if isinstance(fr_data.get("ethos"), dict):
            ethos_score = fr_data["ethos"].get("score")
        if isinstance(fr_data.get("smartFollowers"), dict):
            smart_followers = fr_data["smartFollowers"].get("totalCount") or 0
        if isinstance(fr_data.get("tweetsWithCA"), dict):
            ca_undeleted = fr_data["tweetsWithCA"].get("undeletedCACount") or 0
            ca_deleted = fr_data["tweetsWithCA"].get("deletedCACount") or 0
    tier, score = score_alpha_tier(bio, followers, ethos_score, smart_followers, ca_undeleted, ca_deleted)
    target_env = NOTION_ENV
    try:
        ensure_notion_select_property_for_env(target_env, "Alpha Tier", [
            {"name": "A", "color": "green"},
            {"name": "B", "color": "yellow"},
            {"name": "C", "color": "gray"},
        ])
        nv = load_env(target_env)
        token = nv["NOTION_TOKEN"]
        notion_patch_page(page_id, token, {
            "Alpha Tier": {"select": {"name": tier}},
            "Quality Score": {"number": int(score)},
        })
    except Exception as e:
        print(f"[scout] quality label patch failed: {e}")
    return tier, score


def build_search_lanes(day_key):
    lanes = {
        "arc_base": [
            # ARC playable/app surfaces — highest priority chain
            "arc nft marketplace", "nft marketplace on arc", "arc inscription marketplace",
            "arc inscription project", "inscription on arc", "arc inscription protocol",
            "x402 arc app", "x402 on arc", "arc payment app", "arc x402 payments",
            "arc launchpad", "launchpad on arc", "arc marketplace", "arc wallet app",
            "arc prediction market", "arc agent marketplace", "arc agent protocol",
            "first inscription on arc", "first nft marketplace on arc", "first launchpad on arc",
            "arc testnet app", "building app on arc", "built on arc marketplace",
            # ARC quantum
            "arc quantum", "quantum on arc", "quantum token arc", "quantum infra arc",
            "quantum primitive arc", "quantum experiment arc",
            # ARC meme/collection/early category
            "arc meme", "meme on arc", "arc collection", "collection on arc",
            "arc genesis collection", "arc native meme", "arc native collection",
            "first collection on arc", "first meme on arc", "arc early project",
            # ARC infra/builder
            "arc protocol", "arc ecosystem", "arc infra", "arc dev", "arc builder", "arc stack",
            "building on arc", "deploying on arc", "arc beta", "arc devnet", "arc mainnet soon", "arc primitive",
        ],
        "tempo": [
            # Tempo payments/settlement/x402/mpp focus
            "tempo nft marketplace", "nft marketplace on tempo", "tempo inscription project",
            "inscription on tempo", "tempo launchpad", "launchpad on tempo",
            "tempo x402 app", "x402 on tempo app", "tempo payment app", "tempo payments marketplace",
            "machine payments app tempo", "agentic x402 tempo", "pay per call tempo",
            "pay per agent tempo", "tempo wallet app", "tempo marketplace",
            "first tempo nft", "first inscription on tempo", "first launchpad on tempo",
            "first payment app on tempo", "tempo native wallet", "built on tempo app",
            # Tempo payments/settlement infra
            "tempo payments", "tempo payment rails", "tempo settlement", "tempo streaming payments",
            "tempo micropayments", "tempo programmable payments", "tempo wallet",
            # Tempo mpp/x402
            "tempo mpp", "mpp on tempo", "machine to machine payments tempo",
            "x402 payments app", "inscription with mpp",
            # Tempo early category
            "tempo genesis collection", "tempo native meme", "tempo experiment",
            "tempo protocol", "tempo ecosystem", "tempo infra", "tempo dev", "tempo stack", "building on tempo",
        ],
        "tao_subnet": [
            # TAO core
            "tao subnet", "bittensor subnet", "subnet tao", "subnet bittensor", "tao protocol",
            "tao ecosystem", "building subnet", "launching subnet", "new subnet live",
            "subnet idea", "subnet experiment", "subnet architecture",
            # TAO mechanism
            "subnet design", "subnet mechanism", "subnet incentive", "subnet emissions",
            "tao economics", "bittensor incentive", "validator subnet", "miner subnet", "subnet emissions",
            "subnet registration", "subnet launch", "subnet owner", "subnet dev", "bittensor build",
            # TAO + inscription/meme/nft/early
            "tao inscription", "inscription on tao", "first tao nft", "first bittensor meme",
            "first inscription on tao", "tao collection", "tao meme", "tao genesis collection",
            "subnet wallet", "subnet prediction market",
            # TAO + AI
            "ai on bittensor", "decentralized ai tao", "tao ai agent", "subnet ai", "inference tao",
            "ai agent on tao", "ai inference on tao", "autonomous agent tao",
        ],
        "ai_earn": [
            "crypto ai agent rewards", "onchain ai agent rewards", "ai agent mining crypto",
            "ai agent yield crypto", "agent worknet crypto", "ai agent task rewards",
            "autonomous agent earns crypto", "ai agent payments x402", "agent to agent payments",
            "ai agent revenue protocol", "depin ai agent rewards", "ai agent marketplace crypto",
        ],
        "ai_general": [
            # Web3 AI infra/protocol/app surfaces, not retail/consumer tool accounts
            "onchain ai protocol", "decentralized ai protocol", "ai agent crypto protocol",
            "autonomous agent protocol crypto", "agent marketplace crypto", "ai smart contract agent",
            "verifiable ai crypto", "zkml protocol", "ai inference protocol",
            "decentralized inference protocol", "ai compute marketplace crypto", "ai infra protocol web3",
            "agent framework crypto", "first ai agent protocol", "agent protocol onchain",
            "ai agent on arc", "ai on tempo", "ai on tempo app", "ai on tao", "ai on tao subnet", "ai on subnet", "onchain ai arc", "onchain ai arc app", "decentralized ai tempo", "ai inference tao",
        ],
        "infra_market": [
            "prediction market infra", "wallet tooling web3", "crypto infra builder",
            "perps protocol", "payments crypto builder", "wallet infra web3",
            "launchpad infra", "trading infra crypto", "onchain payments", "defi launchpad",
            "subnet design", "subnet mechanism", "subnet incentive",
            "tao economics", "bittensor incentive",
        ],
        "solana_plays": [
            # Solana secondary — quantum/inscription/primitive only
            "quantum solana", "solana quantum token", "quantum on solana",
            "solana inscription", "inscription solana", "first inscription on solana",
            "solana primitive", "solana experiment", "first solana nft", "first solana meme",
        ],
        "defi_infra": [
            # DeFi protocol/infra builders
            "just launched defi protocol", "new lending protocol crypto", "new perps protocol",
            "building defi protocol", "our protocol just launched", "defi primitive",
            "new dex launch", "new amm protocol", "yield protocol launch",
            "restaking protocol launch", "liquid staking launch", "new bridge protocol",
            "cross-chain protocol launch", "intent protocol crypto", "solver protocol",
        ],
        "nft_gaming_web3": [
            # NFT/Gaming projects (crypto-native only)
            "nft collection launch crypto", "new nft marketplace", "onchain gaming launch",
            "gamefi protocol launch", "play to earn launch", "new nft project crypto",
            "web3 gaming studio", "onchain game launch", "nft marketplace protocol",
        ],
        "depin_compute": [
            # DePIN and compute networks
            "depin protocol launch", "depin network", "decentralized compute",
            "gpu network crypto", "compute marketplace crypto", "data network launch",
            "depin mining", "depin rewards", "wireless depin", "sensor network crypto",
        ],
        "payments_rwa": [
            # Payments and RWA
            "crypto payments protocol", "onchain payments launch", "rwa protocol launch",
            "real world asset tokenization", "stablecoin protocol launch",
            "payment rails crypto", "settlement protocol crypto",
        ],
    }
    # Rotate lane order per run so all thesis areas get fair coverage.
    # Priority lanes always included; order is shuffled per day_key to avoid ARC-first bias.
    import hashlib
    # Keep TAO as a lane, but do not put it in the first preflight slice by
    # default. Main Notion was becoming TAO-heavy; rotate broader theses first.
    priority_lanes = ["arc_base", "tempo", "ai_general", "solana_plays", "defi_infra", "depin_compute"]
    secondary_lanes = ["nft_gaming_web3", "payments_rwa", "ai_earn", "infra_market", "tao_subnet"]
    # Deterministic shuffle based on day_key so each run within same day is consistent
    # but different days get different order
    seed_val = int(hashlib.md5(str(day_key).encode()).hexdigest()[:8], 16)
    import random as _rng
    r = _rng.Random(seed_val)
    r.shuffle(priority_lanes)
    r.shuffle(secondary_lanes)
    order = priority_lanes + secondary_lanes
    return [(lane, lanes[lane]) for lane in order]


def build_retry_lanes(day_key):
    lanes = {
        "arc_base": [
            "arc nft marketplace",
            "arc inscription project",
            "x402 on arc app",
            "arc payment app",
            "arc launchpad",
            "arc prediction market",
            "arc testnet app",
            "built on arc marketplace",
        ],
        "tempo": [
            "tempo nft marketplace",
            "tempo inscription project",
            "tempo payments app",
            "tempo x402 app",
            "pay per agent tempo",
            "first wallet on tempo",
            "tempo launchpad",
        ],
        "tao_subnet": [
            "building bittensor subnet",
            "bittensor subnet builder",
            "tao subnet launch",
            "validator on bittensor",
            "miner on bittensor",
            "bittensor subnet wallet",
            "ai on bittensor",
            "dtao subnet",
        ],
        "ai_earn": [
            "crypto ai agent rewards",
            "ai agent mining crypto",
            "agent worknet crypto",
            "ai agent task rewards",
            "autonomous agent earns crypto",
            "ai agent payments x402",
            "agent to agent payments",
        ],
        "ai_general": [
            "onchain ai agent protocol",
            "autonomous agent protocol crypto",
            "verifiable ai crypto",
            "ai inference protocol crypto",
            "ai agent marketplace crypto",
            "ai on tao subnet",
            "agent framework crypto",
            "x402 agent protocol",
        ],
        "base_ecosystem": [
            "building on base",
            "base defi launch",
            "base nft project",
            "base prediction market",
            "base amm launch",
            "virtuals protocol base",
        ],
        "prediction_markets": [
            "prediction market crypto",
            "polymarket builder",
            "prediction market protocol",
            "event market onchain",
            "betting protocol crypto",
        ],
        "infra_market": [
            "prediction market infra",
            "wallet tooling web3",
            "payments crypto builder",
            "onchain payments",
            "crypto perps protocol",
            "defi protocol launch",
        ],
        "solana_plays": [
            "quantum on solana",
            "solana inscription",
            "solana primitive",
            "solana agent protocol",
            "solana defi launch",
        ],
    }
    order = ["ai_earn", "tempo", "tao_subnet", "ai_general", "prediction_markets", "arc_base", "base_ecosystem", "infra_market", "solana_plays"]
    return [(lane, lanes[lane]) for lane in order]



def load_seed_tier_state():
    path = Path('/root/.openclaw/workspace/memory/alpha_seed_tiers.json')
    if not path.exists():
        return {"seeds": {}}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            data.setdefault("seeds", {})
            return data
    except Exception:
        pass
    return {"seeds": {}}


def save_seed_tier_state(data):
    path = Path('/root/.openclaw/workspace/memory/alpha_seed_tiers.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def seed_tier_for(handle, tier_state):
    rec = (tier_state.get("seeds") or {}).get(str(handle).lower(), {})
    return int(rec.get("tier", 1) or 1)


def apply_seed_tiers_to_order(seeds, tier_state):
    def key(seed):
        rec = (tier_state.get("seeds") or {}).get(str(seed).lower(), {})
        tier = int(rec.get("tier", 1) or 1)
        last = str(rec.get("last_scanned") or "")
        return (tier, last, str(seed).lower())
    return sorted(seeds, key=key)


def note_seed_scan(seed_stats, seed, rejects=0, main_hits=0, search_hits=0):
    k = str(seed).lower().lstrip('@')
    rec = seed_stats.setdefault(k, {"rejects": 0, "main_hits": 0, "search_hits": 0, "scans": 0})
    rec["rejects"] += int(rejects or 0)
    rec["main_hits"] += int(main_hits or 0)
    rec["search_hits"] += int(search_hits or 0)


def update_seed_tiers_after_run(seed_tier_state, seed_stats):
    now_dt = datetime.datetime.now(datetime.UTC)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    day = now_dt.strftime("%Y-%m-%d")
    seeds = seed_tier_state.setdefault("seeds", {})
    changed = 0
    tier2_daily_rejects = int(os.environ.get("ALPHA_SEED_TIER2_DAILY_REJECTS", "120"))
    tier3_daily_rejects = int(os.environ.get("ALPHA_SEED_TIER3_DAILY_REJECTS", "240"))
    ultra_bad_daily_rejects = int(os.environ.get("ALPHA_SEED_ULTRA_BAD_DAILY_REJECTS", "400"))
    min_daily_scans = int(os.environ.get("ALPHA_SEED_TIER_MIN_DAILY_SCANS", "2"))
    upgrade_hit_threshold = int(os.environ.get("ALPHA_SEED_UPGRADE_HITS_PER_DAY", "1"))
    strong_upgrade_hit_threshold = int(os.environ.get("ALPHA_SEED_STRONG_UPGRADE_HITS_PER_DAY", "2"))
    for seed, stats in seed_stats.items():
        rec = seeds.setdefault(seed, {"tier": 1, "bad_streak": 0, "good_hits": 0, "daily": {}})
        daily = rec.setdefault("daily", {})
        if daily.get("day") != day:
            daily.clear()
            daily["day"] = day
            daily["scans"] = 0
            daily["rejects"] = 0
            daily["hits"] = 0
        rec["last_scanned"] = now
        rec["last_rejects"] = int(stats.get("rejects") or 0)
        rec["last_main_hits"] = int(stats.get("main_hits") or 0)
        rec["last_search_hits"] = int(stats.get("search_hits") or 0)
        hits = int(stats.get("main_hits") or 0) + int(stats.get("search_hits") or 0)
        rejects = int(stats.get("rejects") or 0)
        daily["scans"] = int(daily.get("scans") or 0) + 1
        daily["rejects"] = int(daily.get("rejects") or 0) + rejects
        daily["hits"] = int(daily.get("hits") or 0) + hits
        old = int(rec.get("tier", 1) or 1)
        decision = "keep"
        if hits > 0:
            rec["good_hits"] = int(rec.get("good_hits") or 0) + hits
            rec["bad_streak"] = 0
            # Auto-upgrade: one good hit/day pulls a bad seed up one tier;
            # two+ hits/day makes it top-tier again. Let production data decide.
            if daily["hits"] >= strong_upgrade_hit_threshold:
                rec["tier"] = 1
                decision = "strong_upgrade"
            elif daily["hits"] >= upgrade_hit_threshold:
                rec["tier"] = max(1, old - 1)
                decision = "upgrade"
        elif int(daily.get("scans") or 0) >= min_daily_scans:
            # Auto-downgrade only on daily evidence, not one noisy following pull.
            # Approx with 30m cron: a seed usually gets a few scans/day and 20-80 following/scan.
            # OK: <120 daily rejects; bad: >=120; ultra-bad: >=240-400 with zero hits.
            if daily["rejects"] >= ultra_bad_daily_rejects:
                rec["tier"] = 4
                rec["bad_streak"] = int(rec.get("bad_streak") or 0) + 2
                decision = "ultra_downgrade"
            elif daily["rejects"] >= tier3_daily_rejects:
                rec["tier"] = max(old, 3)
                rec["bad_streak"] = int(rec.get("bad_streak") or 0) + 1
                decision = "downgrade_t3"
            elif daily["rejects"] >= tier2_daily_rejects:
                rec["tier"] = max(old, 2)
                rec["bad_streak"] = int(rec.get("bad_streak") or 0) + 1
                decision = "downgrade_t2"
            else:
                rec["bad_streak"] = max(0, int(rec.get("bad_streak") or 0) - 1)
                decision = "healthy_noise"
        rec["last_tier_decision"] = decision
        if int(rec.get("tier", 1) or 1) != old:
            changed += 1
    save_seed_tier_state(seed_tier_state)
    if seed_stats:
        noisy = sorted(seed_stats.items(), key=lambda kv: kv[1].get("rejects",0), reverse=True)[:8]
        print(f"[checkpoint] seed_tier_update | scanned={len(seed_stats)} | changed={changed} | thresholds=t2:{tier2_daily_rejects},t3:{tier3_daily_rejects},ultra:{ultra_bad_daily_rejects},up:{upgrade_hit_threshold},strong_up:{strong_upgrade_hit_threshold} | top_rejects={noisy}")
    return changed

def load_seeds_config():
    default_cfg = {
        "preferred": [
            "chronosontempo", "opentensor", "0xSunRun", "spacepixel", "0xChemistt",
            "0itsali0", "b4kenny", "m0ment0_", "tenacious_ar", "sonofd0nda", "arow299", "kido201196", "braintao", "TempoMetronome",
        ],
        "primary": [
            "notab2d_", "arow299", "kido201196", "sonofd0nda", "Afrectz", "tenacious_ar",
            "chronosontempo", "Glomay22", "Subit_Crypto", "Coira_io",
            "aiia_ro", "DGYC999", "TheBullWeb3", "0xDipKing", "alexxubyte", "sherjilozair",
            "0xSunRun", "spacepixel", "0xChemistt", "opentensor", "braintao", "TempoMetronome",
            "xweth_", "christxbt", "yangxiao214", "RightPitch20", "mrrose__",
            "0itsali0", "b4kenny", "m0ment0_"
        ],
        "disabled": [],
        "excluded_from_scan": ["PR0T0C0IN", "TempoAgent"],
        "seed_groups": [],
        "seed_group_map": {}
    }
    try:
        cfg = default_cfg.copy()
        if os.path.exists(SEEDS_CONFIG_PATH):
            with open(SEEDS_CONFIG_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict):
                # Preserve scalar tuning from seeds.json too (limits/deadline knobs).
                # Previous code only loaded list fields, silently ignoring following limits and scan caps.
                allowed_scalars = {
                    "less_aggressive_following_limit", "default_following_limit",
                    "second_hop_default_limit", "second_hop_less_aggressive_limit",
                    "max_following_before_search", "max_seed_scans_before_search",
                }
                cfg.update({k: v for k, v in data.items() if isinstance(v, list) or k in allowed_scalars})
        group_dir = Path(SEED_GROUPS_DIR)
        if group_dir.exists():
            merged_primary = list(cfg.get("primary", []))
            merged_preferred = list(cfg.get("preferred", []))
            merged_less = list(cfg.get("less_aggressive", []))
            groups = []
            group_map = {}
            for fp in sorted(group_dir.glob("*.json")):
                try:
                    blob = json.loads(fp.read_text())
                except Exception:
                    continue
                # Support both dict {group, preferred, primary} and plain list formats
                if isinstance(blob, list):
                    blob = {"group": fp.stem, "primary": blob, "preferred": [], "less_aggressive": []}
                group = str(blob.get("group") or fp.stem)
                groups.append(group)
                if group == "random_unworth":
                    cfg.setdefault("excluded_from_scan", [])
                    cfg["excluded_from_scan"] = list(dict.fromkeys(cfg["excluded_from_scan"] + list(blob.get("primary", []))))
                    for h in blob.get("primary", []):
                        group_map[h] = group
                    continue
                merged_primary.extend(blob.get("primary", []))
                merged_preferred.extend(blob.get("preferred", []))
                merged_less.extend(blob.get("less_aggressive", []))
                for h in blob.get("primary", []):
                    group_map[h] = group
            cfg["primary"] = list(dict.fromkeys(merged_primary))
            cfg["preferred"] = list(dict.fromkeys(merged_preferred))
            cfg["less_aggressive"] = list(dict.fromkeys(merged_less))
            cfg["seed_groups"] = groups
            cfg["seed_group_map"] = group_map
        return cfg
    except Exception as e:
        print(f"[scout] seeds config load failed: {e}")
    return default_cfg


def rank_primary_seeds(seeds, preferred=None):
    preferred = preferred or []
    preferred_idx = {seed: i for i, seed in enumerate(preferred)}
    return sorted(seeds, key=lambda s: (preferred_idx.get(s, 999), s.lower()))


def group_balanced_seed_order(seeds, group_map, preferred=None):
    """Interleave sectors so AWP/Tempo/ARC/AI/CT all get surface area each run."""
    preferred = preferred or []
    buckets = {}
    for seed in seeds:
        group = (group_map or {}).get(seed, "ungrouped")
        buckets.setdefault(group, []).append(seed)
    for group, vals in list(buckets.items()):
        vals = rank_primary_seeds(list(dict.fromkeys(vals)), preferred=preferred)
        if vals:
            rotate_by = int(time.time() // 1800) % len(vals)
            buckets[group] = vals[rotate_by:] + vals[:rotate_by]
    priority = ["awp_focus", "ai_earn_focus", "tempo_focus", "arc_focus", "prediction_market_focus", "discovered_small", "ai_focus", "nft_focus", "ct_core", "ungrouped"]
    extras = sorted(g for g in buckets if g not in priority)
    group_order = [g for g in priority + extras if buckets.get(g)]
    # Keep Kevin's thesis lanes consistently visible. Older rotation could start
    # the run on CT/discovered/ungrouped lanes, and the Notion cap would fill
    # before AWP/Tempo/TAO/Mine got meaningful surface area.
    # 2026-05-25 calibration: TAO/subnet was snowballing via auto-learned
    # discovered_small -> TAO seeds and filling Main before other theses got
    # surface area. Keep TAO active, but no longer pin it in the fixed head.
    fixed_head = [g for g in ["awp_focus", "ai_earn_focus", "tempo_focus", "arc_focus", "prediction_market_focus", "ai_focus"] if g in group_order]
    floating_tail = [g for g in group_order if g not in fixed_head]
    if floating_tail:
        rotate_group_by = int(time.time() // 3600) % len(floating_tail)
        floating_tail = floating_tail[rotate_group_by:] + floating_tail[:rotate_group_by]
    group_order = fixed_head + floating_tail
    out = []
    while any(buckets.get(g) for g in group_order):
        for g in group_order:
            if buckets.get(g):
                out.append(buckets[g].pop(0))
    return out


def normalize_handle_for_seed(handle):
    return str(handle or "").strip().lstrip("@").split("/")[-1]


def classify_seed_thesis(handle, bio, source_seed=""):
    """Map good newly discovered projects into thesis-separated seed groups."""
    text = " ".join([str(handle or ""), str(bio or ""), str(source_seed or "")]).lower()
    if any(t in text for t in ["awp", "predict worknet", "mine worknet", "worknet", "ocdata"]):
        return "awp_focus", "AWP/Predict/Mine WorkNet-style real-yield AI/crypto plays"
    if any(t in text for t in ["tempo", "metronome", "x402", "mpp"]):
        return "tempo_focus", "Tempo/x402/payment ecosystem seeds"
    if re.search(r"(?<![a-z0-9])arc(?![a-z0-9])", text) or any(t in text for t in ["arc network", "on @arc", "on arc", "quantum", "erc8126"]):
        return "arc_focus", "ARC/quantum/inscription ecosystem seeds"
    if any(t in text for t in ["prediction market", "polymarket", "narrative market", "market maker", "forecast"]):
        return "prediction_market_focus", "Prediction/narrative market seeds"
    if any(t in text for t in ["ai agent", "agentic", "onchain ai", "decentralized ai", "zkml", "inference", "autonomous agent", "agent protocol"]):
        if any(t in text for t in ["earn", "reward", "mining", "miner", "work", "task", "yield", "revenue", "paid", "cashflow"]):
            return "ai_earn_focus", "Early crypto AI agents/protocols with real earning/reward loops"
        return "ai_focus", "AI/agent project seeds"
    if any(t in text for t in ["inscription", "ordinal", "runes", "mint", "nft marketplace"]):
        return "nft_focus", "NFT/inscription/primitive seeds"
    return "discovered_small", "Auto-harvested small crypto project accounts from our discoveries"


def autolearn_seed_candidate(cand, tier=None, qscore=None, fr_data=None):
    """Persist good discoveries as future seeds, separated by thesis.

    Kevin's rule: learn early (<500, especially <200) crypto/Web3 AI/agent/money-play
    projects as seeds; reject web2 AI/SaaS and individual/KOL noise.
    """
    if os.environ.get("ALPHA_AUTOLEARN_SEEDS", "1").lower() in {"0", "false", "off", "no"}:
        return False
    handle = normalize_handle_for_seed(cand.get("sn"))
    if not handle:
        return False
    hkey = handle.lower()
    if hkey in BLOCKED_HANDLES:
        return False
    try:
        followers = int(cand.get("followers") or 0)
    except Exception:
        followers = 999999
    auto_cap = int(os.environ.get("ALPHA_AUTOLEARN_MAX_FOLLOWERS", "500"))
    bio = cand.get("bio") or ""
    source_seed = cand.get("source_seed") or ""
    if playable_project_thesis_score(handle, bio, source_seed)[0] >= 8:
        auto_cap = max(auto_cap, int(os.environ.get("ALPHA_PLAYABLE_PROJECT_FOLLOWER_CAP", "1000")))
    if followers > auto_cap:
        return False
    blob = " ".join([handle, bio, source_seed]).lower()
    web2_ai_bad = [
        "b2b saas", "saas", "productivity", "sales", "crm", "lead generation", "seo",
        "marketing", "recruiting", "customer support", "chatbot", "newsletter", "course"
    ]
    if any(t in blob for t in web2_ai_bad) and not any(t in blob for t in ["crypto", "web3", "onchain", "wallet", "token", "protocol", "defi", "agentic"]):
        return False
    smart_followers = extract_smart_followers(fr_data) if isinstance(fr_data, dict) else int(cand.get("smart_followers") or 0)
    projectish = projectish_signal_score(blob)
    individual = individual_noise_score(blob)
    if individual >= 1 and projectish < 2 and smart_followers < 3:
        return False
    # Only autolearn accounts that look like projects (not personal/KOL)
    if projectish < 2 and smart_followers < 5:
        return False
    # Seed quality threshold: tier A, or tier B with qscore >= 3 + project signals
    if tier not in {"A"} and not (tier == "B" and qscore and qscore >= 3 and projectish >= 1):
        if not (followers <= 300 and smart_followers >= 2 and projectish >= 1):
            return False
    group, description = classify_seed_thesis(handle, bio, source_seed)
    fp = Path(SEED_GROUPS_DIR) / f"{group}.json"
    try:
        if fp.exists():
            data = json.loads(fp.read_text())
        else:
            data = {"group": group, "description": description, "preferred": [], "primary": [], "less_aggressive": []}
        data.setdefault("group", group)
        data.setdefault("description", description)
        data.setdefault("preferred", [])
        data.setdefault("primary", [])
        data.setdefault("less_aggressive", [])
        existing = {str(x).lower().lstrip("@") for x in data.get("primary", [])}
        if hkey in existing:
            return False
        data["primary"].append(handle)
        # Start learned seeds conservatively; if they keep producing good rows, they can be promoted manually/later.
        if hkey not in {str(x).lower().lstrip("@") for x in data.get("less_aggressive", [])}:
            data["less_aggressive"].append(handle)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        print(f"[autolearn] seed_added | @{handle} | group={group} | followers={followers} | tier={tier} | qscore={qscore} | smart_followers={smart_followers}")
        return True
    except Exception as e:
        print(f"[autolearn] seed_add_failed | @{handle} | group={group} | error={e}")
        return False



def load_reference_sets():
    refs = {"positive": [], "negative": []}
    try:
        pos = Path(REFERENCE_SETS_DIR) / "positive_examples.json"
        neg = Path(REFERENCE_SETS_DIR) / "negative_examples.json"
        if pos.exists():
            refs["positive"] = json.loads(pos.read_text()).get("examples", [])
        if neg.exists():
            refs["negative"] = json.loads(neg.read_text()).get("examples", [])
    except Exception as e:
        print(f"[scout] reference set load failed: {e}")
    return refs


def reference_bias_scores(text_blob, source_seed=""):
    refs = load_reference_sets()
    pos_score = 0
    neg_score = 0
    for item in refs.get("positive", []):
        handle = str(item.get("handle") or "").lower()
        if handle and handle in text_blob:
            pos_score += 3
        for sig in item.get("signals", []):
            if str(sig).lower() in text_blob:
                pos_score += 1
        ss = str(item.get("source_seed") or "").lower()
        if ss and ss in source_seed.lower():
            pos_score += 1
    for item in refs.get("negative", []):
        for sig in item.get("anti_signals", []):
            if str(sig).lower() in text_blob:
                neg_score += 1
    return pos_score, neg_score

def canonicalize_source_seed(source_seed):
    seed = str(source_seed or "").strip()
    if not seed:
        return ""
    if seed.startswith(("following:@", "search:", "retry:", "overlap:", "autopromote:")):
        return seed
    if seed.startswith("following:"):
        handle = seed.split(":", 1)[1].strip().lstrip("@")
        return f"following:@{handle}" if handle else ""
    if seed.startswith("hunt2:following:@"):
        handle = seed.split("@", 1)[1].strip()
        return f"legacy:secondhop:@{handle}" if handle else ""
    if seed.startswith("@"):
        return f"legacy:seed:{seed.lstrip('@')}"
    if re.fullmatch(r"[A-Za-z0-9_]{1,32}", seed):
        return f"legacy:seed:{seed}"
    return f"legacy:{seed}"


def normalize_state_sources(state_handles):
    changed = 0
    for meta in state_handles.values():
        if not isinstance(meta, dict):
            continue
        raw = meta.get("source_seed", "")
        norm = canonicalize_source_seed(raw)
        if norm != raw:
            meta["source_seed"] = norm
            changed += 1
    return changed


def second_hop_seed_candidates(state_handles, limit=24):
    candidates = []
    lane_terms = {
        "tempo": ["tempo", "metronome"],
        "tao": ["tao", "bittensor", "subnet", "tensor", "opentensor"],
        "solana": ["sol", "solana", "spl", "pump", "launch", "qpl"],
        "infra": ["wallet", "market", "perp", "onchain", "defi", "protocol", "infra"],
    }
    disabled_second_hop = {str(x).strip().lower() for x in globals().get("DISABLED_SECOND_HOP_SOURCES", set())}
    for handle, meta in state_handles.items():
        if not isinstance(meta, dict):
            continue
        if str(handle).strip().lower().lstrip("@") in BLOCKED_HANDLES:
            continue
        try:
            followers = int(meta.get("followers", 999999) or 999999)
        except Exception:
            followers = 999999
        seed = canonicalize_source_seed(meta.get("source_seed", ""))
        if followers > 500:
            continue
        if not seed:
            continue
        seed_root = seed.split(':')[-1].split(',')[0].strip().lstrip('@').lower()
        if seed_root in BLOCKED_HANDLES:
            continue
        if 'iamheci' in seed.lower():
            continue
        # Only allow second-hop from canonical seed-follow lineage.
        if not seed.startswith("following:@"):
            continue
        try:
            follow_root = seed.split("@", 1)[1].strip().lower()
        except Exception:
            continue
        if follow_root in disabled_second_hop:
            continue
        handle_lower = str(handle).lower()
        lane_score = 0
        for terms in lane_terms.values():
            if any(term in handle_lower for term in terms):
                lane_score += 1
        freshness = str(meta.get("first_seen", "") or meta.get("last_seen", "") or "")
        try:
            freshness_ts = datetime.datetime.fromisoformat(freshness.replace("Z", "+00:00")).timestamp() if freshness else 0
        except Exception:
            freshness_ts = 0
        source_bonus = 1
        follower_band = 0 if followers <= 220 else 1
        candidates.append((source_bonus, -lane_score, follower_band, -freshness_ts, followers, str(handle)))
    candidates.sort()
    return [h for _, _, _, _, _, h in candidates[:limit]]


def candidate_from_user_obj(u, known, keywords, source_seed):
    u = normalize_xreach_user(u)
    sn = (u.get("screenName") or "").strip()
    bio = u.get("description") or ""

    def reject(reason):
        if not sn:
            return None
        followers_val = u.get("followersCount")
        try:
            followers_val = int(followers_val)
        except Exception:
            followers_val = None
        return {
            "rejected": True,
            "handle": sn,
            "followers": followers_val,
            "bio": bio,
            "source_seed": source_seed,
            "reason": reason,
            "created": parse_date(u.get("createdAt")),
        }

    if not sn:
        return None
    sn_key = sn.lower().lstrip("@")
    if sn_key in KEEP_HANDLES:
        pass
    elif sn_key in BLOCKED_HANDLES:
        return reject("blocked_archived_handle")
    if sn in known:
        return reject("known_existing")
        created_str = u.get("createdAt")
        followers_raw = u.get("followersCount")
        try:
            followers = int(followers_raw)
        except Exception:
            followers = 0
        return {
            "sn": sn,
            "followers": followers,
            "bio": bio,
            "created": parse_date(created_str),
            "source_seed": source_seed,
            "wallets": extract_wallets(bio),
        }

    followers = u.get("followersCount")
    try:
        followers = int(followers)
    except Exception:
        return reject("missing_followers")
    name = u.get("name") or ""
    preliminary_blob = " ".join([sn.lower(), str(name or "").lower(), bio.lower(), str(source_seed or "").lower()])
    playable_score, playable_reasons = playable_project_thesis_score(sn, " ".join([name, bio]), source_seed)
    # Main Notion should stay capped at 500 followers.
    # Discovery can be broad in source selection, but accounts above this cap
    # must not enter the main intake path.
    if followers > 500:
        return reject("main_notion_follower_cap")

    loc = (u.get("location") or "").lower()
    bio_lower = bio.lower()
    name_lower = name.lower()
    sn_lower = sn.lower()
    text_blob = " ".join([sn_lower, name_lower, bio_lower]).strip()
    from_seed_graph = source_seed.startswith("@") or source_seed.startswith("following:@")
    if not bio_language_allowed(bio):
        return reject("unsupported_bio_language")
    has_arabic = any('\u0600' <= ch <= '\u06FF' or '\u0750' <= ch <= '\u077F' or '\u08A0' <= ch <= '\u08FF' for ch in bio)

    # 1. Exclude certain locations
    blacklisted_loc_phrases = [
        "nigeria", "lagos", "brazil", "brasil", "sao paulo", "south africa", "kenya"
    ]
    if any(phrase in loc for phrase in blacklisted_loc_phrases):
        return reject("blacklisted_location")

    crypto_core_terms = [
        "crypto", "web3", "onchain", "defi", "nft", "protocol", "infra", "wallet",
        "dex", "amm", "staking", "bridge", "rollup", "zk", "l2", "solidity", "evm",
        "perps", "prediction market", "prediction", "payments", "payment", "mainnet",
        "testnet", "liquidity", "token", "launchpad", "trading", "trader", "tempo",
        "metronome", "bittensor", "tao", "subnet", "subnets", "open tensor", "opentensor", "dtao",
        "inscription", "inscriptions", "inscription protocol", "miner", "validator",
        "solana", "sol", "spl", "polymarket", "blockchain", "settlement", "l1",
        "arc network", "on-chain finance", "cantonnetwork", "bittensor evm", "tao evm",
        "tao casino", "agent registration", "x402", "tempo chain"
    ]

    # 2. Exclude accounts that look like spam/giveaway hunters.
    # Keep this narrow: words like market/launch/mint/wallet are often valid crypto signals.
    spam_keywords = ["giveaway", "airdrop", "bounty", "follow back", "fbb", "100x", "gem", "to the moon", "moonshot", "shill", "testnet", "retroactive", "alpha thread", "raid", "whitelist", "wl spots", "free mint", "mint now", "join discord", "invite contest", "comment done", "like and retweet", "drop your wallet", "farming", "grind", "daily task", "testnet reward", "airdrop hunter", "testnet hunter", "撸毛", "空投", "羊毛", "免费铸造", "白名单"]
    spam_hits = sum(1 for spam in spam_keywords if spam in text_blob)
    strong_crypto_bio_markers = [
        "wallet", "launchpad", "polymarket", "prediction market", "solana", "spl", "tempo",
        "bittensor", "tao", "subnet", "blockchain", "onchain", "crypto-native", "settlement", "l1"
    ]
    strong_crypto_bio_hit = any(marker in text_blob for marker in strong_crypto_bio_markers)
    china_source = is_china_source(source_seed)
    if spam_hits >= 2 and not strong_crypto_bio_hit and not china_source:
        return reject("spam_keywords")
    if from_seed_graph and spam_hits >= 1 and any(bad in text_blob for bad in ["airdrop", "giveaway", "free mint", "mint now", "join discord", "invite contest", "comment done", "like and retweet", "drop your wallet", "farming", "grind", "daily task", "testnet reward", "wl spots", "whitelist"]) and not china_source:
        return reject("spam_keywords")

    generic_builder_terms = [
        "ai", "agent", "agentic", "builder", "building", "research", "researcher",
        "automation", "tool", "tools", "platform", "infra", "dev", "developer", "engineer"
    ]
    early_play_terms = [
        "alpha", "early", "builder", "research", "dev", "developer", "engineer", "tool", "tools",
        "chain", "mainnet", "tge", "testnet", "launchpad", "mint", "inscription", "nft", "defi",
        "memecoin", "ponzu", "agent", "agentic", "ai", "agi", "infra", "wallet", "liquidity",
        "narrative", "mover", "caller", "ct", "bullpost", "trench", "trenches"
    ]
    negative_non_crypto_terms = [
        "accounting", "bookkeeping", "real estate", "property management", "housing",
        "mortgage", "crm", "lead generation", "marketing", "seo", "ads", "sales ops",
        "hr", "recruiting", "talent", "productivity", "b2b saas", "saas", "fintech",
        "saas ai", "ai startup", "openai wrapper", "chatgpt tool", "ai productivity",
        "ai marketing", "ai automation agency", "b2b ai saas", "generic ai saas", "ai for business",
        "healthcare", "clinic", "ecommerce", "shopify", "restaurant", "logistics",
        "mindset", "personal growth", "discipline", "self improvement", "daily mindset",
        "motivation", "healing", "massage", "spa", "salon", "beauty", "wellness",
        "coach", "coaching", "therapist", "therapy", "product hunt", "build in public",
        "tutorial", "how to", "sports", "sport", "gamer", "gaming", "baseball", "football",
        "lifestyle", "quotes", "inspiration", "preparation is", "abu dhabi", "berkeley, ca",
        "alpha caller", "airdrop hunter", "testnet hunter", "community growth", "kol", "influencer",
        "local business", "booking", "appointment", "actorrise", "scenepartner",
        "serving retail", "retail via x", "retail users", "paid tool your agent needs"
    ]
    strong_handle_terms = [
        "tempo", "metronome", "tao", "bittensor", "subnet", "tensor", "opentensor",
        "inscription", "xbt", "onchain", "defi", "wallet", "perp", "polymarket", "arc"
    ]
    project_account_markers = [
        "protocol", "app", "platform", "wallet", "launchpad", "marketplace", "dex", "amm",
        "infra", "sdk", "api", "docs", "mainnet", "devnet", "testnet", "network", "chain",
        "l1", "rollup", "bridge", "payments", "settlement", "prediction market", "tooling",
        "builder", "building on", "built on", "powered by", "on @tempo", "on tempo", "on @arc",
        "on arc", "on @bittensor", "on bittensor", "subnet", "inscription", "quantum", "agent",
        "domains", "mint", "nft marketplace", "launching on", "live on", "launched on"
    ]
    personal_noise_markers = [
        "trader", "daytrader", "investor", "enthusiast", "shitposting", "alpha", "daily alpha",
        "early gems", "gem hunter", "degen", "designer", "artist", "analyst",
        "follow for", "dms open", "threads", "content", "kol", "influencer", "personal account",
        "head of bd", "bd ", "maximalist", "professional anon", "mum's basement", "come fuck me",
        "aggregator", "bot", "gateway to nfts", "on-chain culture"
    ]

    keyword_hits = sum(1 for k in keywords if k in text_blob)
    crypto_hits_bio = sum(1 for t in crypto_core_terms if t in bio_lower)
    crypto_hits_name = sum(1 for t in crypto_core_terms if t in (sn_lower + " " + name_lower))
    crypto_hits_total = sum(1 for t in crypto_core_terms if t in text_blob)
    generic_hits = sum(1 for t in generic_builder_terms if t in text_blob)
    early_play_hits = sum(1 for t in early_play_terms if t in text_blob)
    non_crypto_hits = sum(1 for t in negative_non_crypto_terms if t in text_blob)
    strong_handle_hit = any(t in sn_lower or t in name_lower for t in strong_handle_terms)
    project_marker_hits = sum(1 for t in project_account_markers if t in text_blob)
    personal_noise_hits = sum(1 for t in personal_noise_markers if t in text_blob)
    likely_project_account = project_marker_hits >= 2 or (
        any(x in text_blob for x in ["protocol", "launchpad", "wallet", "marketplace", "payments", "subnet", "inscription", "network", "chain", "sdk", "api", "docs"])
        and crypto_hits_total >= 2
    )
    seed_strength = source_seed.count(",") + (1 if from_seed_graph else 0)
    graph_strong = seed_strength >= 2 or source_seed.startswith("overlap:")
    bio_empty = len(bio_lower.strip()) < 4

    # Hard reject: Arabic / giveaway / empty-or-non-crypto profiles should not pass even from following.
    if has_arabic and crypto_hits_total == 0:
        return reject("arabic_non_crypto_profile")
    if has_arabic and any(x in text_blob for x in ["giveaway", "airdrop", "follow back", "free mint", "wl", "whitelist", "drop your wallet", "comment done"]):
        return reject("arabic_giveaway_profile")
    if bio_empty and project_marker_hits == 0 and crypto_hits_total == 0 and not strong_handle_hit:
        return reject("empty_non_crypto_profile")
    if crypto_hits_total == 0 and project_marker_hits == 0 and not strong_handle_hit:
        weak_non_crypto_profile_terms = [
            "anime", "gamer", "youtuber", "collector", "artist", "designer", "follow back",
            "personal account", "coffee", "travel", "lifestyle", "motivation", "quotes", "family"
        ]
        if any(x in text_blob for x in weak_non_crypto_profile_terms) or len(text_blob.strip()) < 24:
            return reject("non_crypto_profile")

    # Reject obvious non-crypto AI/SaaS / self-help / local-business / random-personal profiles.
    if non_crypto_hits >= 1 and crypto_hits_total == 0 and not strong_handle_hit:
        return reject("non_crypto_profile")
    if non_crypto_hits >= 2 and crypto_hits_total <= 1:
        return reject("too_many_non_crypto_signals")

    # Generic builder/AI wording alone is not enough.
    if generic_hits >= 2 and crypto_hits_total == 0:
        return reject("generic_builder_without_crypto")

    # Random/personal profiles with weak bios should not pass just because of graph/following paths.
    if crypto_hits_total == 0 and not strong_handle_hit:
        weak_personal_markers = [
            "preparation is", "gamer", "tech enthusiast", "building ideas into products",
            "frontend developer", "web dev", "passionate", "queens, ny", "abu dhabi"
        ]
        if bio_empty or any(m in text_blob for m in weak_personal_markers):
            return reject("weak_random_personal_profile")

    # Project-only bias disabled: crypto-native people/accounts can still pass.
    if source_seed.startswith("autopromote:"):
        active_thesis_autopromote = any(x in text_blob for x in [
            "arc", "tempo", "tao", "bittensor", "subnet", "onchain ai", "agentic", "x402", "mpp", "payments", "inscription", "quantum", "awp", "predict", "mining"
        ])
        if not (likely_project_account and active_thesis_autopromote and project_marker_hits >= 2):
            return reject("autopromote_not_project")

    # following:@seed is useful provenance, but it is not sufficient proof on its own.
    # These graph-sourced candidates must still clear the normal project/crypto evidence gates below.
    if source_seed.startswith("following:@"):
        clear_personal_noise = any(x in text_blob for x in ["trader", "daytrader", "investor", "gem hunter", "daily alpha", "degen", "analyst", "researcher", "follow for", "dms open", "kol", "influencer", "personal account", "shitposting", "designer", "artist", "enthusiast", "maximalist", "professional anon", "content creator", "writer", "macro"])
        projectish_signal = any(x in text_blob for x in ["protocol", "app", "platform", "wallet", "launchpad", "marketplace", "dex", "amm", "infra", "sdk", "api", "docs", "network", "chain", "payments", "settlement", "prediction market", "subnet", "agent", "built on", "powered by", "mainnet", "devnet", "beta", "live"])
        weak_following_patterns = any(x in text_blob for x in ["enjoyer", "content creator", "writer", "macro", "calm analysis", "community manager", "research & writing", "head of", "co-founder"])
        if clear_personal_noise and not (projectish_signal or crypto_hits_total >= 1):
            return reject("personal_profile_not_project")
        if weak_following_patterns and project_marker_hits == 0 and crypto_hits_total == 0:
            return reject("personal_profile_not_project")
        if bio_empty and not strong_handle_hit and project_marker_hits == 0 and crypto_hits_total == 0:
            return reject("weak_random_personal_profile")

    accepted = False
    explicit_crypto_bio_phrases = [
        "prediction market", "polymarket", "crypto-native", "non-custodial crypto wallets",
        "wallet", "launchpad", "on @tempo", "on tempo", "on @solana", "on solana",
        "blockchain", "rollup settlement", "l1 blockchain", "sol wagering", "token protocol",
        "arc network", "on-chain finance", "cantonnetwork", "bittensor evm", "tao evm",
        "tao casino", "agent registration", "x402 powered", "tempo chain", "continuous clearing auctions",
        "built on @base", "built on base", "virtuals protocol", "on-chain ai agent verification"
    ]
    # Project-specific acceptance patterns (early alpha projects)
    arclens_arc_pattern = bool(re.search(r'arclens|arc\s*network|arc\s*protocol', text_blob))
    onrails_finance_pattern = bool(re.search(r'onrails|on\s*rails\s*finance', text_blob))
    erc8126_pattern = bool(re.search(r'erc[\s-]?8126|8126', text_blob))
    tempo_product_pattern = bool(re.search(r'tempo\s*(product|launchpad|protocol|app|platform)', text_blob))
    tao_evm_pattern = bool(re.search(r'tao\s*evm|tao\s*(chain|network|layer)', text_blob))
    auction_market_pattern = bool(re.search(r'continuous clearing auctions|auction[s]?|bid, and claim|prediction market|narrative market', text_blob))
    onchain_agent_security_pattern = bool(re.search(r'on-?chain ai agent verification|agent verification|virtuals protocol|built on @base|built on base', text_blob))
    thesis_hits = sum([arclens_arc_pattern, onrails_finance_pattern, erc8126_pattern, tempo_product_pattern, tao_evm_pattern, auction_market_pattern, onchain_agent_security_pattern])

    # Accept graph-backed niche accounts even with thin bio only if handle/name is strong
    # and the profile is not screaming generic non-crypto.
    if any(p in bio_lower for p in explicit_crypto_bio_phrases):
        accepted = True
    elif any([arclens_arc_pattern, onrails_finance_pattern, erc8126_pattern, tempo_product_pattern, tao_evm_pattern, auction_market_pattern, onchain_agent_security_pattern]):
        accepted = True
    elif from_seed_graph and not source_seed.startswith("following:@") and strong_handle_hit and non_crypto_hits == 0 and personal_noise_hits == 0 and (crypto_hits_total >= 1 or project_marker_hits >= 1):
        accepted = True
    elif graph_strong and not source_seed.startswith("following:@") and followers <= (220 if thesis_hits >= 1 else 120) and non_crypto_hits == 0 and not any(bad in text_blob for bad in ["saas", "therapy", "football", "lifestyle", "real estate", "clinic", "coffee", "speech-to-text", "ecosystem"]):
        accepted = True
    # Accept if bio itself is crypto-explicit.
    elif crypto_hits_bio >= (1 if thesis_hits >= 1 else 2) and (followers <= 220 or thesis_hits >= 1):
        accepted = True
    # Accept if bio + handle/name together show enough crypto proof.
    elif crypto_hits_total >= 2 and crypto_hits_name >= 1:
        accepted = True
    # Accept if name/handle itself is strong and there is at least one keyword signal.
    elif strong_handle_hit and (keyword_hits >= 1 or from_seed_graph) and (crypto_hits_total >= 1 or thesis_hits >= 1 or project_marker_hits >= 1) and (followers <= 180 or thesis_hits >= 1):
        accepted = True
    # Accept if there are multiple total crypto signals across handle/name/bio.
    elif crypto_hits_total >= 3 and (project_marker_hits >= 1 or thesis_hits >= 1 or strong_handle_hit or (from_seed_graph and followers <= 180)):
        accepted = True
    # Open crypto base gate: any crypto signal + no non-crypto noise + English = pass.
    # No thesis/project_marker/seed_graph requirement for general crypto accounts.
    elif crypto_hits_total >= 1 and non_crypto_hits == 0 and not has_arabic and not bio_empty:
        accepted = True
    # Even with empty-ish bio, accept if handle/name is crypto-ish
    elif crypto_hits_total >= 1 and non_crypto_hits == 0 and not has_arabic and (strong_handle_hit or crypto_hits_name >= 1):
        accepted = True
    # Bio/name weak? allow only if tweets confirm crypto signal.
    elif has_recent_crypto_tweet_signal(sn, keywords):
        accepted = True

    if accepted and source_seed.startswith("search:tempo:"):
        weak_tempo_search = (
            bio_empty
            or (keyword_hits == 0 and project_marker_hits < 2)
            or not any(p in text_blob for p in [
                "tempo", "metronome", "wallet", "launchpad", "prediction market",
                "onchain", "protocol", "nft", "mint", "payments", "x402", "mpp"
            ])
        )
        if weak_tempo_search:
            accepted = False

    if accepted and source_seed.startswith("search:tao_subnet:"):
        weak_tao_search = (
            bio_empty
            or (keyword_hits == 0 and project_marker_hits < 2)
            or not any(p in text_blob for p in [
                "tao", "bittensor", "subnet", "opentensor", "dtao",
                "validator", "miner", "evm", "protocol", "wallet", "marketplace", "inference"
            ])
        )
        if weak_tao_search:
            accepted = False

    if accepted and source_seed.startswith("search:arc_base:"):
        weak_arc_search = (
            bio_empty
            or (keyword_hits == 0 and project_marker_hits < 2)
            or not any(p in text_blob for p in [
                "arc", "base", "x402", "payments", "wallet", "protocol",
                "onchain", "prediction market", "inscription", "network", "quantum", "launchpad", "marketplace"
            ])
        )
        if weak_arc_search:
            accepted = False

    if accepted and source_seed.startswith("search:solana_plays:"):
        weak_solana_search = (
            bio_empty
            or not any(p in text_blob for p in ["inscription", "quantum", "primitive"])
        )
        if weak_solana_search:
            accepted = False

    if accepted and source_seed.startswith("search:ai_general:"):
        weak_ai_search = (
            bio_empty
            or (keyword_hits == 0 and project_marker_hits < 2)
            or not any(p in text_blob for p in [
                "onchain", "defi", "wallet", "protocol", "agent", "agentic",
                "crypto", "web3", "prediction market", "bittensor", "tempo", "arc", "zkml", "inference"
            ])
        )
        if weak_ai_search:
            accepted = False

    if accepted and source_seed.startswith("search:"):
        lane_name = source_seed.split(":", 1)[1].split(":")[0] if ":" in source_seed else "unknown"
        lane_known_counts[lane_name] = lane_known_counts.get(lane_name, 0) + 1

    if not accepted and crypto_hits_total == 0 and non_crypto_hits > 0:
        return reject("insufficient_crypto_proof")

    # Guard against weak one-word accidental matches unless graph-backed or handle-strong.
    if not from_seed_graph and not strong_handle_hit:
        if crypto_hits_total == 1 and generic_hits == 0 and "alpha" not in text_blob and "early" not in text_blob:
            if not has_recent_crypto_tweet_signal(sn, keywords):
                return reject("weak_single_signal_without_tweet_confirmation")

    # If bio is empty, only allow if graph-backed and handle/name strongly signals the niche,
    # or if there is strong overlap graph evidence and tweets confirm crypto signal.
    if bio_empty and not ((from_seed_graph and strong_handle_hit and non_crypto_hits == 0) or (graph_strong and has_recent_crypto_tweet_signal(sn, keywords))):
        return reject("empty_bio_without_strong_crypto_proof")

    created_str = u.get("createdAt")
    return {
        "sn": sn,
        "followers": followers,
        "bio": bio,
        "created": parse_date(created_str),
        "source_seed": source_seed,
        "wallets": extract_wallets(bio),
        "playable_score": playable_score,
        "playable_reasons": playable_reasons[:5],
    }


def is_main_notion_candidate(cand):
    bio_raw = cand.get("bio") or ""
    bio = bio_raw.lower()
    source_seed = (cand.get("source_seed") or "").lower()
    profile_blob = " ".join([str(cand.get("sn") or "").lower(), bio])
    text_blob = profile_blob
    try:
        followers = int(cand.get("followers") or 0)
    except Exception:
        followers = 0
    playable_score, playable_reasons = playable_project_thesis_score(cand.get("sn"), bio_raw, source_seed)
    cand["playable_score"] = playable_score
    cand["playable_reasons"] = playable_reasons[:5]
    # Main Notion cap: keep accounts at or below 500 followers.
    # Priority can still favor <500 and especially <200, but >1000 should not
    # pass the main gate.
    if followers > 500:
        return False
    if not bio_language_allowed(bio_raw):
        return False

    # Main Notion gate should only remove obvious trash after follower cap.
    spam_blockers = ["airdrop", "giveaway", "free mint", "mint now", "join discord", "invite contest", "comment done", "like and retweet", "drop your wallet", "farming", "grind", "daily task", "testnet reward", "wl spots", "whitelist", "#airdrop", "#giveaway", "claim now"]
    pos_bias, neg_bias = reference_bias_scores(text_blob, source_seed)
    if any(bad in text_blob for bad in spam_blockers):
        return False
    if neg_bias >= 2 and pos_bias == 0:
        return False

    retail_or_core_blockers = [
        "serving retail", "retail via x", "paid tool your agent needs",
        "founder @", "founder at http", "founder & ceo", "board member", "building & investing",
        "incentivizing intelligence", "research house focused", "former tech reporter",
        "based bitcoiner", "cat mom", "testing products", "staying early",
        "if it brings value", "connect if you're building", "quantitative strategist",
        "security researcher", "hodl #eth", "hodl #sui", "web3 participant",
        "investment researcher", "sharing people and things", "builder at @", "dev: @",
        "breaking crypto", "market moves", "market news", "daily market updates", "crypto news",
        "news & research", "research in minutes", "newsletter", "guides and tutorials",
        "for all experience levels", "open to promos", "open to collabs", "content creator",
        "researcher @", "member @", "builder & learner", "sharing tools & insights"
    ]
    if any(bad in text_blob for bad in retail_or_core_blockers):
        cand["reject_reason"] = "recent_personal_or_generic_profile"
        return False

    # Hard guard for TAO personal/KOL/noise that made Main dirty again.
    # TAO/subnet stays supported, but personal holdings/calls/owner-only accounts
    # must go to Reject unless they are clearly a project/product/protocol.
    tao_personal_noise = (
        any(t in profile_blob for t in ["$tao", "#tao", "bittensor", "subnet", "tao"])
        and any(t in profile_blob for t in ["i hold", "all in", "good calls", "owner of", "founder of @", "crypto twitter account", "meme survivor", "research logs", "testnet notes", "music and technology nerd"])
        and not any(t in profile_blob for t in ["protocol", "platform", "network", "app", "tooling", "dashboard", "infrastructure", "service layer", "training", "evaluation", "miner and validator tooling", "stablecoin", "market", "prediction", "settlement"])
    )
    if tao_personal_noise:
        cand["reject_reason"] = "tao_personal_noise_not_project"
        return False

    precision_score, precision_reasons = pre_accept_quality_score(profile_blob, source_seed)
    # Calibrated precision layer: project/builder accounts only.
    # Following sources need higher bar (lots of noise); search is pre-filtered by thesis keywords.
    min_precision_score = 1 if source_seed.startswith("following:@") else 1
    urgent_following = any(tag in source_seed for tag in [
        'following:@0xdetweiler', 'following:@0xleonidas', 'following:@0xwhacko', 'following:@aezchain',
        'following:@apeinafarm', 'following:@bearzverse', 'following:@christxbt', 'following:@crypto_goatinho',
        'following:@cryptogoatinho', 'following:@hbh_zhr', 'following:@honka_yo', 'following:@honkayo',
        'following:@ibraxbt', 'following:@impossiblemach', 'following:@kido201196', 'following:@mrrose',
        'following:@mutu_1987', 'following:@rightpitch20', 'following:@sonofd0nda', 'following:@unsynned',
        'following:@web3gus', 'following:@xweth', 'following:@yangxiao214',
        'following:@nonfungiblemomo', 'following:@nickplayscrypto', 'following:@office2crypto', 'following:@moitreghason', 'following:@amplifi_now', 'following:@td_0101', 'following:@3dmax_virtuals', 'following:@gkisokay', 'following:@bigwil', 'following:@goon_crypto', 'following:@100xdarren', 'following:@idaratbn', 'following:@biig_classic1', 'following:@qinglong0791', 'following:@siyabaldacc', 'following:@skywalker_t25', 'following:@0xalphanur', 'following:@richard_ola_', 'following:@btcmonie', 'following:@nikitont', 'following:@xiaobi988', 'following:@emilylweb3', 'following:@boo_man69', 'following:@trathoa', 'following:@b4kenny', 'following:@0itsali0', 'following:@m0ment0_', 'following:@arow299', 'following:@dazzlercoin', 'following:@tenacious_ar', 'following:@notab2d_', 'following:@afrectz', 'following:@tma_420', 'following:@canducrypto', 'following:@sulu0x', 'following:@xiaohao67942591', 'following:@kimmykim_nft', 'following:@santiagoroel', 'following:@mayamaster', 'following:@thacryptoninja', 'following:@pepedrt', 'following:@0xlaughing', 'following:@iamheci', 'following:@0xgeeky'
    ])
    if urgent_following:
        min_precision_score = 0
    if playable_score >= 8:
        min_precision_score = min(min_precision_score, 1)
    # Obvious crypto-project rescue before precision reject. These were showing up
    # as false positives in Reject DB: Bittensor subnet builders, onchain infra,
    # protocol/tooling accounts with very low followers.
    obvious_project_rescue = (
        bool(re.search(r"\bsn\s*\d+\b|bittensor\s+sn\s*\d+|subnet\s*\d+|tensorusd|opentensor|dtao", profile_blob))
        or any(p in profile_blob for p in [
            "bittensor ecosystem", "building on bittensor", "native stablecoin layer",
            "blockchain cap table", "institutional-grade infrastructure", "onchain economy",
            "protocol engineering", "dev updates", "sdk", "tooling", "developers",
            "data-driven storytelling for the onchain economy", "onchain analytics",
        ])
    )
    if precision_score < min_precision_score and not obvious_project_rescue:
        cand["reject_reason"] = "precision_gate_reject"
        cand["precision_score"] = precision_score
        cand["precision_reasons"] = precision_reasons[:6]
        print(f"[checkpoint] precision_gate_reject | @{cand.get('sn','')} | score={precision_score}/{min_precision_score} | reasons={','.join(precision_reasons[:6])} | source={source_seed}")
        return False

    projectish_score = projectish_signal_score(profile_blob)
    individual_score = individual_noise_score(profile_blob)
    smart_followers = int(cand.get("smart_followers") or 0)
    if obvious_project_rescue and projectish_score >= 2 and individual_score <= 1 and neg_bias < 2:
        cand["accept_reason"] = "obvious_crypto_project_rescue"
        return True
    # Strict: reject personal/trader/learner profiles unless clearly a project.
    # Bittensor/infra builders can mention founder/builder/designer but still be
    # project-relevant; let the rescue signals above pass them.
    if individual_score >= 1 and projectish_score < 2 and smart_followers < 4 and not urgent_following and not obvious_project_rescue:
        cand["reject_reason"] = "individual_profile_not_project"
        return False
    if individual_score >= 2 and projectish_score < 3 and smart_followers < 6 and not urgent_following:
        cand["reject_reason"] = "high_individual_not_project"
        return False
    if source_seed.startswith("search:") and projectish_score < 2:
        cand["reject_reason"] = "search_result_not_projectish"
        return False
    if source_seed.startswith("search:") and individual_score > 0 and smart_followers < 6:
        cand["reject_reason"] = "search_result_individual_noise"
        return False

    crypto_passthrough_terms = [
        "crypto", "web3", "onchain", "defi", "nft", "wallet", "launchpad", "marketplace",
        "prediction market", "polymarket", "narrative market", "trading", "perps", "dex",
        "protocol", "infra", "appchain", "rollup", "l2", "zk", "bridge", "settlement",
        "payments", "base", "solana", "ethereum", "bitcoin", "btc", "eth", "tao",
        "bittensor", "subnet", "tempo", "arc", "awp", "predict", "mining", "inscription",
        "ordinal", "runes", "agent", "agentic", "ai agent", "onchain ai", "virtuals",
        "evm", "compiler", "runtime", "validator", "miner", "staking", "yield",
        "liquidity", "swap", "amm", "token", "mint", "forge", "chain"
    ]
    source_crypto_bias = any(t in source_seed for t in [
        "polymarket", "prediction", "market", "crypto", "defi", "wallet", "launchpad",
        "base", "solana", "tao", "bittensor", "subnet", "tempo", "arc", "awp", "predict"
    ])
    crypto_passthrough_hits = sum(1 for t in crypto_passthrough_terms if t in profile_blob)
    if source_seed.startswith("autopromote:") and (crypto_passthrough_hits >= 1 or source_crypto_bias) and neg_bias < 2:
        return True
    # Broad crypto alone is not enough for main; require thesis/action unless very strong.
    if crypto_passthrough_hits >= 3 and ("arc" in profile_blob or "tempo" in profile_blob or "tao" in profile_blob or "bittensor" in profile_blob or "subnet" in profile_blob or "x402" in profile_blob or "inscription" in profile_blob or "quantum" in profile_blob or "onchain ai" in profile_blob or "decentralized ai" in profile_blob or "ai agent" in profile_blob or "awp" in profile_blob or "predict" in profile_blob) and neg_bias < 2:
        return True
    # Pre-compute action/narrative hits needed for following graph gate below.
    _action_terms_early = ["shipped", "deployed", "launched", "live", "built", "building", "minting", "staking", "farming", "yield", "liquidity"]
    _narrative_terms_early = ["inscription", "quantum", "payments", "settlement", "subnet", "agent", "agentic", "x402", "mpp", "prediction market", "polymarket"]
    action_hits = sum(1 for t in _action_terms_early if t in profile_blob)
    narrative_hits = sum(1 for t in _narrative_terms_early if t in profile_blob)
    # Following graph calibration: if this is clearly a crypto/project/product account and not
    # an individual/KOL, allow it even outside ARC/Tempo/TAO. This keeps Main Notion broad.
    if (
        source_seed.startswith("following:@")
        and crypto_passthrough_hits >= 3
        and projectish_score >= 2
        and individual_score == 0
        and (strong_builder_signal(profile_blob) or action_hits >= 1 or narrative_hits >= 1)
        and neg_bias < 2
    ):
        cand["accept_reason"] = "following_broad_crypto_project"
        return True

    if source_seed.startswith("following:@"):
        arabic_chars = sum(1 for ch in bio_raw if '\u0600' <= ch <= '\u06FF' or '\u0750' <= ch <= '\u077F' or '\u08A0' <= ch <= '\u08FF')
        cjk_chars = sum(1 for ch in bio_raw if '\u4E00' <= ch <= '\u9FFF')
        ascii_letters = sum(1 for ch in bio_raw if ('a' <= ch.lower() <= 'z'))
        non_ascii_letters = sum(1 for ch in bio_raw if ch.isalpha() and ord(ch) > 127)
        total_letters = ascii_letters + non_ascii_letters
        if arabic_chars >= 3:
            return False
        if cjk_chars >= 2 and not any(k in text_blob for k in ["crypto", "web3", "onchain", "defi", "nft", "wallet", "launchpad", "inscription", "agent", "ai", "bittensor", "tao", "tempo", "arc"]):
            return False
        if total_letters >= 8 and ascii_letters < max(6, total_letters // 2) and not any(k in text_blob for k in ["crypto", "web3", "onchain", "defi", "nft", "wallet", "launchpad", "inscription", "agent", "ai", "bittensor", "tao", "tempo", "arc"]):
            return False
        if ascii_letters == 0 and total_letters >= 4 and not any(k in text_blob for k in ["crypto", "web3", "onchain", "defi", "nft", "wallet", "launchpad", "inscription", "agent", "ai", "bittensor", "tao", "tempo", "arc"]):
            return False

    chain_terms = [
        "arc", "tempo", "metronome", "tao", "bittensor", "subnet", "opentensor",
        "x402", "mpp", "base", "virtuals", "solana", "awp", "predict", "mining",
        "bitcoin", "ethereum", "eth", "btc", "ordinal", "runes", "符文",
        "agent", "agentic", "ai inference", "decentralized ai", "protocol", "infra",
        "onchain", "web3", "defi", "nft", "layer2", "l2", "rollup", "zk",
        "evm", "compiler", "runtime", "forge", "chain", "swap", "amm", "token"
    ]
    narrative_terms = [
        "payments", "settlement", "wallet", "launchpad", "marketplace", "prediction market",
        "inscription", "quantum", "agent", "agentic", "onchain ai", "decentralized ai",
        "ai inference", "ai protocol", "subnet", "validator", "miner", "protocol", "infra",
        "awp", "predict", "mining",
    ]
    action_terms = [
        "build", "building", "launch", "launching", "launched", "live", "beta", "devnet",
        "mainnet", "genesis", "first", "native", "primitive", "experiment", "experimental",
        "deployed", "shipping", "shipped", "docs", "sdk", "api",
        "minting", "staking", "farming", "yield", "liquidity"
    ]

    chain_hits = sum(1 for t in chain_terms if t in profile_blob)
    narrative_hits = sum(1 for t in narrative_terms if t in profile_blob)
    action_hits = sum(1 for t in action_terms if t in profile_blob)
    narrative_hits += min(2, pos_bias)
    action_hits += 1 if pos_bias >= 2 else 0
    thesis_core_terms = [
        "arc", "tempo", "x402", "mpp", "tao", "bittensor", "subnet", "opentensor",
        "awp", "predict", "prediction market", "polymarket", "onchain ai", "ai agent",
        "agentic", "decentralized ai", "zkml", "ai inference", "quantum", "inscription",
        "ordinal", "runes", "primitive", "genesis", "devnet", "mainnet soon"
    ]
    thesis_core_hits = sum(1 for t in thesis_core_terms if t in profile_blob)
    thesis_source = any(tag in source_seed for tag in [
        "search:arc_base:", "search:tempo:", "search:tao_subnet:", "search:ai_general:",
        "retry:arc_base:", "retry:tempo:", "retry:tao_subnet:", "retry:ai_general:",
        "following:@awp_protocol", "following:@tempo", "following:@chronosontempo", "following:@tempoagent",
    ])

    # Crypto-native bypass: if account has strong crypto signals, skip strict gate
    crypto_core = sum(1 for t in ["crypto", "web3", "onchain", "defi", "nft", "wallet", "protocol",
                                   "bitcoin", "ethereum", "btc", "eth", "arc", "tempo", "tao",
                                   "bittensor", "subnet", "agent", "ai", "mining", "awp", "predict",
                                   "inscription", "ordinal", "rollup", "l2", "zk", "bridge", "dex",
                                   "evm", "solana", "compiler", "swap", "amm", "token", "chain"]
                      if t in profile_blob)

    # Crypto-native bypass must be thesis-adjacent. Broad Base/Solana/Ethereum alone is not enough.
    if thesis_core_hits >= 1 and crypto_core >= 3 and (narrative_hits >= 1 or action_hits >= 1 or strong_builder_signal(profile_blob)) and neg_bias < 2:
        return True
    if thesis_source and crypto_core >= 2 and (narrative_hits >= 1 or action_hits >= 1 or strong_builder_signal(profile_blob)) and neg_bias < 2:
        return True

    strong_phrase_pass = any(p in profile_blob for p in [
        "prediction market", "continuous clearing auctions", "built on @base", "built on base", "awp protocol", "awp mining", "awp predict",
        "virtuals protocol", "on-chain ai agent verification", "tempo payments", "tempo x402",
        "tao subnet", "bittensor subnet", "arc inscription", "arc quantum", "inscription on arc",
        "quantum on arc", "ai on tao", "decentralized ai tao", "ai agent on tao",
        "bitcoin ordinal", "ordinal inscription", "runes protocol", "btc l2", "ethereum l2",
        "ai agent", "agentic ai", "onchain agent", "web3 infra"
    ])

    if thesis_core_hits >= 1 and chain_hits >= 1 and narrative_hits >= 1 and neg_bias < 3:
        return True
    if thesis_core_hits >= 2:
        return True
    if thesis_core_hits >= 1 and narrative_hits >= 2 and action_hits >= 1:
        return True
    if thesis_source and chain_hits >= 1 and (narrative_hits >= 1 or action_hits >= 1) and neg_bias < 3:
        return True
    if strong_phrase_pass:
        return True
    if playable_score >= 8 and neg_bias < 3:
        return True
    if smart_followers >= 3 and (projectish_score >= 1 or thesis_core_hits >= 1 or thesis_source) and individual_score == 0 and neg_bias < 2:
        return True

    # Kevin calibration 2026-05-08: thesis lanes are priority, not a cage.
    # If following/search finds a real crypto/web3 PROJECT account (not an individual/KOL),
    # allow it into Main even outside ARC/Tempo/TAO/onchain-AI. Keep the bar project-shaped
    # so broad CT users still go to Reject.
    broad_project_crypto_hits = sum(1 for t in [
        "crypto", "web3", "onchain", "on-chain", "defi", "dex", "amm", "swap", "liquidity",
        "nft", "wallet", "protocol", "infra", "infrastructure", "marketplace", "launchpad",
        "lending", "borrow", "credit", "yield", "vault", "bridge", "privacy", "identity",
        "security", "pentesting", "rpc", "indexer", "portfolio tracker", "self-custody",
        "passkeys", "onchain recovery", "gamefi", "onchain game", "on-chain game",
        "prediction market", "payments", "settlement", "agent", "agents", "decentralized"
    ] if t in profile_blob)
    if (
        broad_project_crypto_hits >= 3
        and projectish_score >= 2
        and individual_score == 0
        and (strong_builder_signal(profile_blob) or action_hits >= 1 or narrative_hits >= 1)
        and neg_bias < 2
        and not any(bad in text_blob for bad in spam_blockers)
    ):
        cand["accept_reason"] = "broad_crypto_project"
        return True
    if (
        broad_project_crypto_hits >= 3
        and projectish_score >= 1
        and individual_score == 0
        and (strong_builder_signal(profile_blob) or action_hits >= 1)
        and neg_bias < 2
    ):
        cand["accept_reason"] = "broad_crypto_project_builder"
        return True
    return False


def promote_candidate_to_notion(cand, known):
    preflight_fr_data = None
    if not is_main_notion_candidate(cand):
        # Use a tiny cached/live frontrun preflight only for borderline project/thesis candidates.
        # This lets strong smart-follower evidence rescue zero-post/thin project accounts without bypassing quotas.
        if preflight_frontrun_eligible(cand):
            fr, cache_hit = run_frontrun_cached(cand.get("sn"), allow_live=True)
            preflight_fr_data = fr.get("data", {}) if fr and fr.get("ok") else None
            if preflight_fr_data and smart_follower_override_allowed(cand, preflight_fr_data):
                cand["smart_followers"] = extract_smart_followers(preflight_fr_data)
                cand["reject_reason"] = None
                print(f"[checkpoint] smart_follower_preaccept | @{cand.get('sn')} | smart_followers={cand['smart_followers']} | cache={cache_hit} | source={cand.get('source_seed','')}")
        if not preflight_fr_data or not is_main_notion_candidate(cand):
            print(f"[checkpoint] main_notion_gate_reject | @{cand['sn']} | source={cand.get('source_seed','')}")
            return False, False, {
            'handle': cand.get('sn'),
            'followers': cand.get('followers') or 0,
            'bio': cand.get('bio') or '',
            'source': cand.get('source_seed') or '',
            'reason': cand.get('reject_reason') or 'main_notion_thesis_gate',
            'precision_score': cand.get('precision_score'),
            'precision_reasons': cand.get('precision_reasons'),
        }
    handle_url = f"https://x.com/{cand['sn']}"
    page_id, page_url, created = push_to_notion(
        handle_url=handle_url,
        bio=cand["bio"],
        account_created=cand["created"],
        followers=cand["followers"],
        smart_follower=False,
        source_seed=cand["source_seed"],
        wallets=cand["wallets"],
    )
    if not page_id:
        return False, False, None
    if not created:
        known.setdefault(cand["sn"], {}).update({
            "last_seen": datetime.datetime.now(datetime.UTC).isoformat(),
            "followers": cand["followers"],
            "notion_url": page_url,
            "source_seed": cand["source_seed"],
            "wallets": cand["wallets"],
            "page_id": page_id,
        })
        return True, False, None
    print(f"[scout] added {handle_url} ({cand['followers']} followers) -> {page_url}")
    known[cand["sn"]] = {
        "first_seen": datetime.datetime.now(datetime.UTC).isoformat(),
        "followers": cand["followers"],
        "notion_url": page_url,
        "source_seed": cand["source_seed"],
        "wallets": cand["wallets"],
        "page_id": page_id,
    }
    if preflight_fr_data:
        fr_data = preflight_fr_data
    else:
        fr = run_frontrun_cached(cand["sn"], allow_live=True)[0]
        fr_data = fr.get("data", {}) if fr and fr.get("ok") else None
    if fr_data:
        token = load_env(NOTION_ENV).get("NOTION_TOKEN", "")
        if token and enrich_with_frontrun(page_id, token, fr_data):
            print(f"[enrich] frontrun data added for {cand['sn']}")
    tier, qscore = apply_quality_labels(page_id, cand["bio"], cand["followers"], fr_data, source_seed=cand.get("source_seed", ""))
    print(f"[quality] {cand['sn']} -> tier={tier} score={qscore}")
    autolearn_seed_candidate(cand, tier=tier, qscore=qscore, fr_data=fr_data)
    time.sleep(0.4)
    return True, True, None


def write_sniper_rule_logs(ts, success_records, othersalpha_records):
    # Best-effort optional logging hook. Keep non-fatal for cron reliability.
    return False


def main():
    global BLOCKED_HANDLES, KEEP_HANDLES, BAD_TWITTER_POOL_LABELS, TWITTER_POOL_INDEX, XREACH_RATE_LIMITED, RATE_LIMIT_HITS, SOFT_RATE_LIMIT_MODE
    BAD_TWITTER_POOL_LABELS = set()
    BAD_TWITTER_POOL_STRIKES.clear()
    TWITTER_POOL_INDEX = 0
    XREACH_RATE_LIMITED = False
    RATE_LIMIT_HITS = 0
    SOFT_RATE_LIMIT_MODE = False
    state = load_state()
    known = state.get("handles", {})
    BLOCKED_HANDLES = load_blocked_handles()
    KEEP_HANDLES = load_keep_handles()
    normalized_state_sources = normalize_state_sources(known)

    keywords = [
        "ai", "tempo", "nft", "web3", "early", "alpha", "builder", "research", "agent",
        "agentic", "onchain", "crypto", "defi", "infra", "protocol", "llm", "mcp", "autonomous",
        "testnet", "mainnet", "token", "dex", "amm", "yield", "staking", "bridge",
        "wallet", "trading", "trader", "prediction market", "perps", "launchpad",
        "rollup", "zk", "l2", "solidity", "evm", "payment", "payments", "liquidity",
        "bittensor", "tao", "subnet", "subnets", "open tensor", "opentensor",
        "dtao", "alpha tao", "tao ecosystem", "tao agent", "tao infra",
        "inscription", "inscriptions", "inscription protocol", "miner", "validator",
        "solana", "sol", "spl", "pump", "memecoin", "quantum", "qpl", "launch"
    ]
    seeds_cfg = load_seeds_config()
    seed_tier_state = load_seed_tier_state()
    seed_run_stats = {}
    seeds = [s for s in seeds_cfg.get("primary", []) if s not in set(seeds_cfg.get("disabled", []))]

    # keep de-duplicated seed order in case future edits accidentally repeat handles
    seeds = rank_primary_seeds(list(dict.fromkeys(seeds)), preferred=seeds_cfg.get("preferred", []))

    if os.environ.get("ALPHA_DRY_RUN") == "1":
        print("[checkpoint] ALPHA_DRY_RUN=1 | script loaded and configuration initialized; skipping network/discovery")
        print(f"[checkpoint] dry_run_config | seeds={len(seeds)} | min_new={MIN_NEW_PER_RUN} | max_new={MAX_NEW_PER_RUN}")
        return

    ensure_notion_select_property("Alpha Tier", [
        {"name": "A", "color": "green"},
        {"name": "B", "color": "yellow"},
        {"name": "C", "color": "gray"},
    ])
    ensure_notion_number_property("Quality Score")

    max_new_per_run = MAX_NEW_PER_RUN
    target_min_new = MIN_NEW_PER_RUN
    run_started_at = time.monotonic()
    stop_reason = None
    new_count = 0
    following_added = 0
    search_added = 0
    seeds_scanned = 0
    queries_scanned = 0
    query_failures = 0
    othersalpha_records = []
    success_records = []
    rejected_reason_counts = {}
    lane_item_counts = {}
    lane_reject_counts = {}
    lane_known_counts = {}

    print(f"[checkpoint] start | known={len(known)} | seeds={len(seeds)} | groups={seeds_cfg.get('seed_groups', [])} | min_new={target_min_new} | max_new={max_new_per_run} | pool={len(load_twitter_pool())} | rate_limit_threshold={RATE_LIMIT_THRESHOLD}")

    bad_seed_markers = set(seeds_cfg.get("excluded_from_scan", []))
    ordered_seeds = [s for s in seeds if s not in bad_seed_markers]
    # Sector-balanced rotation: don't let noisy CT/global seeds starve AWP/Tempo/ARC/AI lanes.
    ordered_seeds = group_balanced_seed_order(ordered_seeds, seeds_cfg.get("seed_group_map", {}), preferred=seeds_cfg.get("preferred", []))
    ordered_seeds = apply_seed_tiers_to_order(ordered_seeds, seed_tier_state)
    less_aggressive = set(x.lower() for x in seeds_cfg.get("less_aggressive", []))
    default_following_limit = int(seeds_cfg.get("default_following_limit", 30))
    less_following_limit = int(seeds_cfg.get("less_aggressive_following_limit", 20))
    second_hop_default_limit = int(seeds_cfg.get("second_hop_default_limit", 30))
    second_hop_less_limit = int(seeds_cfg.get("second_hop_less_aggressive_limit", 15))
    max_following_before_search = int(seeds_cfg.get("max_following_before_search", min(8, max_new_per_run)))
    if max_following_before_search <= 0:
        max_following_before_search = max_new_per_run
    max_following_before_search = min(max_new_per_run, max_following_before_search)

    def deadline_near():
        # Leave enough wall-clock for state save, summary write, and a bounded
        # reject Notion sync before OpenClaw's hard SIGTERM window.
        return (time.monotonic() - run_started_at) >= max(60, RUN_DEADLINE_S - FINALIZE_DEADLINE_BUFFER_S)

    def target_reached():
        # Stop promptly once the configured minimum is satisfied. The previous
        # behavior kept scanning/enriching toward MAX_NEW_PER_RUN, which exceeds
        # heartbeat/cron runtimes after broad crypto gate loosening.
        return new_count >= target_min_new or new_count >= max_new_per_run

    def should_stop_discovery():
        nonlocal stop_reason
        if target_reached():
            stop_reason = stop_reason or "target_reached"
            return True
        if XREACH_RATE_LIMITED and (RATE_LIMIT_HITS >= RATE_LIMIT_THRESHOLD or (load_twitter_pool() and len(BAD_TWITTER_POOL_LABELS) >= len(load_twitter_pool()))):
            stop_reason = stop_reason or "xreach_rate_limited"
            print(f"[checkpoint] xreach_unavailable_stop | reason=rate_limited | hits={RATE_LIMIT_HITS} | bad={len(BAD_TWITTER_POOL_LABELS)} | elapsed={int(time.monotonic() - run_started_at)}s | new={new_count}")
            return True
        pool_size = len(load_twitter_pool())
        if pool_size and len(BAD_TWITTER_POOL_LABELS) >= pool_size:
            stop_reason = stop_reason or "xreach_cookie_pool_exhausted"
            print(f"[checkpoint] xreach_unavailable_stop | reason=cookie_pool_exhausted | bad={len(BAD_TWITTER_POOL_LABELS)} | pool={pool_size} | elapsed={int(time.monotonic() - run_started_at)}s | new={new_count}")
            return True
        if deadline_near():
            stop_reason = stop_reason or "deadline_near"
            print(f"[checkpoint] deadline_near | elapsed={int(time.monotonic() - run_started_at)}s | new={new_count} | finalizing=1")
            return True
        return False

    def run_search_lanes(lanes, route_label="search"):
        """Run thesis search lanes before/beside following so ARC/Tempo/TAO/AI thesis doesn't get starved by noisy follow graph."""
        nonlocal new_count, search_added, queries_scanned, query_failures
        for lane_name, queries in lanes:
            if should_stop_discovery():
                break
            for q in queries:
                if should_stop_discovery():
                    break
                queries_scanned += 1
                res = run_xreach(["search", q, "--type", "latest", "-n", str(SEARCH_RETRY_N), "--json"])
                if not isinstance(res, dict):
                    query_failures += 1
                    print(f"[checkpoint] {route_label}_query_fail | lane={lane_name} | {q}")
                    if query_failures >= SEARCH_FAIL_FAST_THRESHOLD:
                        print(f"[checkpoint] {route_label}_search_fail_fast | query_failures={query_failures} | lane={lane_name}")
                        return
                    continue
                for item in res.get("items", []):
                    if target_reached():
                        break
                    u = hydrate_search_user(item.get("user") or item.get("author") or {}, item.get("id"))
                    cand = candidate_from_user_obj(u, known, keywords, f"{route_label}:{lane_name}:{q}")
                    if not cand:
                        continue
                    if cand.get("rejected"):
                        if should_record_othersalpha(cand):
                            othersalpha_records.append({
                                "handle": cand.get("handle"),
                                "followers": cand.get("followers"),
                                "source": cand.get("source_seed"),
                                "reason": cand.get("reason"),
                                "bio": cand.get("bio"),
                                "created": cand.get("created"),
                            })
                        continue
                    ok, created, reject_rec = promote_candidate_to_notion(cand, known)
                    if ok and created:
                        new_count += 1
                        search_added += 1
                    elif reject_rec and should_record_othersalpha({'rejected': True, **reject_rec}):
                        othersalpha_records.append(reject_rec)

    # Following-first calibration (Kevin 2026-05-15): Main Notion was getting too ARC/search-heavy.
    # Keep thesis search, but make it a small optional preflight so seed-following crypto graphs
    # get most of the discovery budget. Set ALPHA_THESIS_PREFLIGHT_Q_PER_LANE=0 to disable.
    if not should_stop_discovery() and os.environ.get("ALPHA_SKIP_THESIS_PREFLIGHT", "0") != "1":
        day_key = f"{datetime.datetime.now(datetime.UTC).timetuple().tm_yday}_{datetime.datetime.now(datetime.UTC).hour}_{datetime.datetime.now(datetime.UTC).minute // 30}"
        raw_thesis_lanes = build_search_lanes(day_key)[:6]
        # Do not blast every query every run; rotate a tiny subset per lane to avoid search dominating.
        q_per_lane = int(os.environ.get("ALPHA_THESIS_PREFLIGHT_Q_PER_LANE", "1"))
        if q_per_lane <= 0:
            raw_thesis_lanes = []
        rotate_slot = int(time.time() // 1800)  # aligned with 30m cron cadence
        thesis_lanes = []
        for lane_name, queries in raw_thesis_lanes:
            if not queries:
                thesis_lanes.append((lane_name, []))
                continue
            start = (rotate_slot * q_per_lane) % len(queries)
            picked = [queries[(start + i) % len(queries)] for i in range(min(q_per_lane, len(queries)))]
            thesis_lanes.append((lane_name, picked))
        print(f"[checkpoint] thesis_preflight_search | lanes={len(thesis_lanes)} | q_per_lane={q_per_lane} | current_new={new_count}")
        run_search_lanes(thesis_lanes, route_label="search")

    # Primary discovery path: followings of seed accounts
    skip_following = os.environ.get("ALPHA_SKIP_FOLLOWING", "") == "1"
    max_seed_scans_before_search = int(os.environ.get("ALPHA_MAX_SEED_SCANS_BEFORE_SEARCH", str(seeds_cfg.get("max_seed_scans_before_search", 30))))
    if skip_following:
        print("[checkpoint] skip_following_phase | reason=ALPHA_SKIP_FOLLOWING=1")
    for seed in ordered_seeds:
        if skip_following:
            break
        if should_stop_discovery() or following_added >= max_following_before_search or seeds_scanned >= max_seed_scans_before_search:
            break
        seeds_scanned += 1
        seed_group = seeds_cfg.get("seed_group_map", {}).get(seed, "ungrouped")
        seed_tier = seed_tier_for(seed, seed_tier_state)
        following_limit = less_following_limit if seed.lower() in less_aggressive else default_following_limit
        if seed_tier >= 2:
            following_limit = min(following_limit, less_following_limit)
        if seed_tier >= 3:
            following_limit = min(following_limit, int(os.environ.get("ALPHA_TIER3_FOLLOWING_LIMIT", "8")))
        if seed_tier >= 4:
            following_limit = min(following_limit, int(os.environ.get("ALPHA_TIER4_FOLLOWING_LIMIT", "3")))
        # Preferred seeds (top producers) get higher limits unless auto-demoted by repeated low yield
        if seed_tier == 1 and seed.lower() in {s.lower() for s in seeds_cfg.get("preferred_seeds", seeds_cfg.get("preferred", []))}:
            following_limit = max(following_limit, 80)
        if seed_group == 'user_urgent_seeds':
            following_limit = max(following_limit, int(os.environ.get('ALPHA_URGENT_SEED_FOLLOWING_LIMIT', '60')))
        if seed_group == "awp_focus":
            # AWP ecosystem is still sparse; mine the source graph deeper than generic CT seeds.
            following_limit = int(os.environ.get("ALPHA_AWP_FOLLOWING_LIMIT", "50")) if seed.lower() == "awp_protocol" else max(following_limit, int(os.environ.get("ALPHA_AWP_SECONDARY_FOLLOWING_LIMIT", "20")))
        print(f"[checkpoint] seed_run | @{seed} | group={seed_group} | tier={seed_tier} | following_limit={following_limit}")
        seed_rejects_before = len(othersalpha_records)
        seed_hits_before = following_added
        res = run_xreach(["following", seed, "-n", str(following_limit), "--json"])
        if not isinstance(res, dict):
            print(f"[checkpoint] seed_fail | @{seed}")
            continue
        for u in res.get("items", []):
            if should_stop_discovery() or following_added >= max_following_before_search:
                break
            cand = candidate_from_user_obj(u, known, keywords, f"following:@{seed}")
            if not cand:
                continue
            if cand.get("rejected"):
                if should_record_othersalpha(cand):
                    othersalpha_records.append({
                        "handle": cand.get("handle"),
                        "followers": cand.get("followers"),
                        "source": cand.get("source_seed"),
                        "reason": cand.get("reason"),
                        "bio": cand.get("bio"),
                    })
                continue
            if "sn" not in cand:
                print(f"[checkpoint] malformed_candidate_skip | source={cand.get('source_seed')} | keys={sorted(cand.keys())}")
                continue
            ok, created, reject_rec = promote_candidate_to_notion(cand, known)
            if ok:
                if created:
                    new_count += 1
                    following_added += 1
                continue
            if reject_rec and should_record_othersalpha({'rejected': True, **reject_rec}):
                othersalpha_records.append(reject_rec)
        note_seed_scan(seed_run_stats, seed, rejects=len(othersalpha_records) - seed_rejects_before, main_hits=following_added - seed_hits_before)

    # Second-hop discovery path: use previously found small accounts as fresh seed surfaces
    urgent_mode = os.environ.get("ALPHA_URGENT_FOLLOWING_ONLY", "1").lower() not in {"0", "false", "off", "no"}
    hop_seeds = [] if urgent_mode else second_hop_seed_candidates(state.get("handles", {}), limit=16)
    for seed in hop_seeds:
        if should_stop_discovery() or following_added >= max_following_before_search:
            break
        seeds_scanned += 1
        hop_limit = second_hop_less_limit if seed.lower() in less_aggressive else second_hop_default_limit
        res = run_xreach(["following", seed, "-n", str(hop_limit), "--json"])
        if not isinstance(res, dict):
            print(f"[checkpoint] second_hop_seed_fail | @{seed}")
            continue
        for u in res.get("items", []):
            if should_stop_discovery() or following_added >= max_following_before_search:
                break
            cand = candidate_from_user_obj(u, known, keywords, f"following:@{seed}")
            if not cand:
                continue
            if cand.get("rejected"):
                if should_record_othersalpha(cand):
                    othersalpha_records.append({
                        "handle": cand.get("handle"),
                        "followers": cand.get("followers"),
                        "source": cand.get("source_seed"),
                        "reason": cand.get("reason"),
                        "bio": cand.get("bio"),
                    })
                continue
            if "sn" not in cand:
                print(f"[checkpoint] malformed_candidate_skip | source={cand.get('source_seed')} | keys={sorted(cand.keys())}")
                continue
            ok, created, reject_rec = promote_candidate_to_notion(cand, known)
            if ok:
                if created:
                    new_count += 1
                    following_added += 1
                continue
            if reject_rec and should_record_othersalpha({'rejected': True, **reject_rec}):
                othersalpha_records.append(reject_rec)

    # Overlap-follow discovery: harvest handles that appear across multiple seed/hop follow surfaces
    overlap_sources = [] if urgent_mode else (ordered_seeds[:8] + hop_seeds[:8])
    overlap_seen = {}
    overlap_users = {}
    for seed in overlap_sources:
        if should_stop_discovery() or following_added >= max_following_before_search:
            break
        seeds_scanned += 1
        res = run_xreach(["following", seed, "-n", "50", "--json"])
        if not isinstance(res, dict):
            print(f"[checkpoint] overlap_seed_fail | @{seed}")
            continue
        for u in res.get("items", []):
            sn = (u.get("screenName") or "").strip()
            if not sn or sn in known:
                continue
            overlap_seen.setdefault(sn, set()).add(seed)
            overlap_users.setdefault(sn, u)

    ranked_overlap = sorted(overlap_seen.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    for sn, srcs in ranked_overlap:
        if should_stop_discovery() or following_added >= max_following_before_search:
            break
        if len(srcs) < 2:
            continue
        u = overlap_users.get(sn) or {}
        cand = candidate_from_user_obj(u, known, keywords, f"overlap:{','.join(sorted(srcs))[:180]}")
        if not cand:
            continue
        if cand.get("rejected"):
            othersalpha_records.append({
                "handle": cand.get("handle"),
                "followers": cand.get("followers"),
                "source": cand.get("source_seed"),
                "reason": cand.get("reason"),
                "bio": cand.get("bio"),
            })
            continue
        if "sn" not in cand:
            print(f"[checkpoint] malformed_candidate_skip | source={cand.get('source_seed', source_seed if 'source_seed' in locals() else '')} | keys={sorted(cand.keys())}")
            continue
        ok, created, reject_rec = promote_candidate_to_notion(cand, known)
        if ok:
            if created:
                success_records.append({
                    "sn": cand["sn"],
                    "followers": cand.get("followers", 0),
                    "source_seed": cand.get("source_seed", ""),
                    "route": "china" if is_china_source(cand.get("source_seed", "")) else "main",
                    "notion_url": known.get(cand["sn"], {}).get("notion_url", ""),
                })
                new_count += 1
                following_added += 1
            continue
        if reject_rec and should_record_othersalpha({'rejected': True, **reject_rec}):
            othersalpha_records.append(reject_rec)

    if (not urgent_mode) and new_count < target_min_new and not SOFT_RATE_LIMIT_MODE and XREACH_CONSECUTIVE_FAILURES < max(3, XREACH_FAILURE_STOP_THRESHOLD // 2) and not (load_twitter_pool() and len(BAD_TWITTER_POOL_LABELS) >= len(load_twitter_pool())):
        # Reset rate limit flag so search gets a fair chance even if following had timeouts
        if XREACH_RATE_LIMITED and not (load_twitter_pool() and len(BAD_TWITTER_POOL_LABELS) >= len(load_twitter_pool())):
            print(f"[checkpoint] search_reset_rate_limit | was_rate_limited=true | reason=give_search_fair_chance")
            XREACH_RATE_LIMITED = False
        day_key = f"{datetime.datetime.now(datetime.UTC).timetuple().tm_yday}_{datetime.datetime.now(datetime.UTC).hour}_{datetime.datetime.now(datetime.UTC).minute // 30}"
        retry_lanes = build_retry_lanes(day_key)
        print(f"[checkpoint] retry_round | current_new={new_count} | target_min={target_min_new} | lanes={len(retry_lanes)}")
        for lane_name, queries in retry_lanes:
            if should_stop_discovery():
                break
            for q in queries:
                if should_stop_discovery():
                    break
                queries_scanned += 1
                retry_n = str(SEARCH_RETRY_N)
                res = run_xreach(["search", q, "--type", "latest", "-n", retry_n, "--json"])
                if not isinstance(res, dict):
                    query_failures += 1
                    print(f"[checkpoint] retry_query_fail | lane={lane_name} | {q}")
                    if should_stop_discovery():
                        break
                    if query_failures >= SEARCH_FAIL_FAST_THRESHOLD:
                        print(f"[checkpoint] retry_search_fail_fast | query_failures={query_failures} | lane={lane_name}")
                        break
                    continue
                for item in res.get("items", []):
                    if target_reached():
                        break
                    u = hydrate_search_user(item.get("user") or item.get("author") or {}, item.get("id"))
                    cand = candidate_from_user_obj(u, known, keywords, f"retry:{lane_name}:{q}")
                    if not cand:
                        continue
                    if cand.get("rejected"):
                        if should_record_othersalpha(cand):
                            othersalpha_records.append({
                                "handle": cand.get("handle"),
                                "followers": cand.get("followers"),
                                "source": cand.get("source_seed"),
                                "reason": cand.get("reason"),
                                "bio": cand.get("bio"),
                                "created": cand.get("created"),
                            })
                        continue
                    if "sn" not in cand:
                        print(f"[checkpoint] malformed_candidate_skip | source={cand.get('source_seed')} | keys={sorted(cand.keys())}")
                        continue
                    ok, created, reject_rec = promote_candidate_to_notion(cand, known)
                    if ok:
                        if created:
                            new_count += 1
                            search_added += 1
                        continue
                    if reject_rec and should_record_othersalpha({'rejected': True, **reject_rec}):
                        othersalpha_records.append(reject_rec)

    autopromoted = 0
    autopromote_seen = set()
    autopromote_records = [] if urgent_mode else othersalpha_records
    for rec in autopromote_records:
        if should_stop_discovery():
            break
        handle = (rec.get("handle") or "").strip()
        if not handle or handle in known or handle.lower() in autopromote_seen:
            continue
        autopromote_seen.add(handle.lower())
        if not should_autopromote_othersalpha(rec):
            continue
        cand = {
            "sn": handle,
            "followers": rec.get("followers") or 0,
            "bio": rec.get("bio") or "",
            "created": rec.get("created"),
            "source_seed": f"autopromote:{rec.get('source') or ''}",
            "wallets": extract_wallets(rec.get("bio") or ""),
        }
        ok, created, reject_rec = promote_candidate_to_notion(cand, known)
        if ok and created:
            autopromoted += 1
        elif reject_rec and should_record_othersalpha({'rejected': True, **reject_rec}):
            othersalpha_records.append(reject_rec)

    for rec in othersalpha_records:
        reason = (rec.get("reason") or "unknown").strip() or "unknown"
        if reason == "missing_followers":
            continue
        rejected_reason_counts[reason] = rejected_reason_counts.get(reason, 0) + 1
        source = (rec.get("source") or "").strip()
        lane = source.split(":", 1)[0] if ":" in source else source
        if lane:
            lane_reject_counts[lane] = lane_reject_counts.get(lane, 0) + 1

    state["handles"] = known
    save_state(state)
    append_othersalpha(othersalpha_records)
    reject_created, reject_updated = 0, 0
    final_error = None
    reject_push_deadline_ts = run_started_at + max(60, RUN_DEADLINE_S - 60)
    rejects_to_push = select_rejects_for_push(othersalpha_records, MAX_REJECTS_PUSH_PER_RUN)
    if len(othersalpha_records) > len(rejects_to_push):
        unique_sources = len({(r.get("source") or r.get("source_seed") or "unknown") for r in othersalpha_records})
        print(f"[checkpoint] reject_push_capped | total={len(othersalpha_records)} | pushing={len(rejects_to_push)} | fair_sources={unique_sources}")
    try:
        reject_created, reject_updated = push_rejects_to_notion(rejects_to_push, deadline_ts=reject_push_deadline_ts)
    except Exception as e:
        final_error = f"push_rejects_to_notion_failed: {e}"
        print(f"[scout] final reject push failed: {e}")
    run_summary = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        "seeds_scanned": seeds_scanned,
        "queries_scanned": queries_scanned,
        "query_failures": query_failures,
        "following_added": following_added,
        "search_added": search_added,
        "total_new": new_count,
        "known_now": len(known),
        "othersalpha": len(othersalpha_records),
        "reject_notion_created": reject_created,
        "reject_notion_updated": reject_updated,
        "autopromoted": autopromoted,
        "rejected_reason_counts": rejected_reason_counts,
        "lane_item_counts": lane_known_counts,
        "lane_reject_counts": lane_reject_counts,
        "lane_known_counts": lane_known_counts,
        "xreach_rate_limited": XREACH_RATE_LIMITED,
        "soft_rate_limit_mode": SOFT_RATE_LIMIT_MODE,
        "stop_reason": stop_reason,
        "elapsed_seconds": int(time.monotonic() - run_started_at),
        "final_error": final_error,
    }
    append_run_summary(run_summary)
    try:
        write_sniper_rule_logs(run_summary.get("ts"), success_records, othersalpha_records)
    except Exception as e:
        print(f"[scout] write_sniper_rule_logs failed: {e}")
    # Update seed tier state based on this run's performance
    try:
        update_seed_tiers_after_run(seed_tier_state, seed_run_stats)
    except Exception as e:
        print(f"[scout] update_seed_tiers_after_run failed: {e}")
    print(f"[checkpoint] finish | seeds_scanned={seeds_scanned} | queries_scanned={queries_scanned} | query_failures={query_failures} | following_added={following_added} | search_added={search_added} | total_new={new_count} | known_now={len(known)} | othersalpha={len(othersalpha_records)} | reject_notion_created={reject_created} | reject_notion_updated={reject_updated} | autopromoted={autopromoted}")
    print(f"[checkpoint] reject_breakdown | {json.dumps(rejected_reason_counts, ensure_ascii=False, sort_keys=True)}")
    print(f"[checkpoint] lane_known | {json.dumps(lane_known_counts, ensure_ascii=False, sort_keys=True)}")
    print(f"[scout] discovered {new_count} new alpha accounts")


def _hard_timeout_handler(signum, frame):
    summary = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        "status": "run_aborted",
        "reason": "hard_timeout_no_finish",
        "deadline_seconds": int(os.environ.get("ALPHA_SCOUT_HARD_TIMEOUT_S", "1200")),
        "hard_cap_enforced": True,
        "constraints": {
            "main_notion_follower_cap": 500,
            "crypto_only": True,
            "awp_thesis": True,
            "hard_rejects": ["non_crypto", "arabic_giveaway", "empty_profiles", "weak_personal_following_leaks"],
        },
        "notion_added_count": 0,
        "othersalpha_count": 0,
        "top_reject_sources_reasons": [],
        "final_error": "process exceeded hard timeout before finish checkpoint",
    }
    try:
        append_run_summary(summary)
        print(f"[checkpoint] hard_timeout_abort | deadline={summary['deadline_seconds']}s | summary_written=1")
    except Exception as e:
        print(f"[checkpoint] hard_timeout_abort | summary_write_failed={e}")
    raise SystemExit(124)


if __name__ == "__main__":
    hard_timeout_s = int(os.environ.get("ALPHA_SCOUT_HARD_TIMEOUT_S", "1200"))
    if hasattr(signal, "SIGALRM") and hard_timeout_s > 0:
        signal.signal(signal.SIGALRM, _hard_timeout_handler)
        signal.alarm(hard_timeout_s)
    try:
        main()
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
