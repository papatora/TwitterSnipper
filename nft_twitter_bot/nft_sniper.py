"""
NFT Sniper X — Recent Following Monitor (Adaptive)

Cara kerja:
  Tiap interval → untuk setiap CT seed, ambil N recent following (bukan crawl habis)
  → diff dengan state sebelumnya → proses hanya yang baru di-follow

Adaptive rotation:
  - Seed diurutkan berdasarkan yield terakhir (paling produktif duluan)
  - Seed aktif (banyak new follow) → dapat FETCH lebih banyak (bonus)
  - Seed stale (banyak dupe) → tetap dicek tapi dengan jatah minimum
  - Semua seed selalu dicek tiap cycle (tidak ada yang di-skip)

Gate dedup (3 lapis):
  Gate 1: in sniper_seen.txt     → skip (sudah di Notion)
  Gate 2: in sniper_rejected.txt → skip (pernah reject)
  Gate 3: filter NFT             → reject / process

Usage:
  python nft_sniper.py                    # sekali (untuk cron)
  python nft_sniper.py --loop             # loop otomatis tiap INTERVAL_MIN menit
  python nft_sniper.py --loop --interval 20
"""

import argparse
import os
import json
import re
import time
import logging
import datetime
from pathlib import Path
import requests

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sniper_live.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

COOKIE_FILES = [p for p in os.environ.get("NFT_COOKIE_FILES", "").split(":") if p]
# Example: export NFT_COOKIE_FILES=/path/auth1.txt:/path/auth2.txt
STATIC_PROXY_FILE   = "/root/.openclaw/workspace/NFTtwitterbot/NFTtwitterbot/proxies_static.txt"
ROTATING_PROXY_FILE = "/root/.openclaw/workspace/NFTtwitterbot/NFTtwitterbot/proxy.txt"
CT_NFT_FILE         = "/root/.openclaw/workspace/NFTtwitterbot/NFTtwitterbot/nftseed.txt"
STATE_FILE          = "/root/.openclaw/workspace/NFTtwitterbot/NFTtwitterbot/runtime/sniper_state.json"

BEARER = os.environ.get("TWITTER_BEARER", "")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DB_ID = "ccb633a6-5089-4982-a158-c479864b72f9"

# Reject DB — CT accounts yang tidak lolos NFT gate (untuk manual review)
REJECT_NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
REJECT_NOTION_DB_ID = "33e878f48f9880a89558d548026ac91e"

FRONTRUN_TOKEN = os.environ.get("FRONTRUN_TOKEN", "")
FRONTRUN_BASE  = "https://loadbalance.frontrun.pro"

BASE_CT_LIST = [
    "supraEVM", "0itsali0", "jianganti876719", "fly963136",
    "mahonebtc", "Alraiany2", "Tma_420", "tenacious_ar",
]

INTERVAL_MIN           = 20    # default loop interval (menit)
FETCH_PER_SEED         = 15    # berapa recent following yang diambil per seed per run
FOLLOWERS_CAP          = 500   # max followers untuk masuk list (early accounts only)
FRONTRUN_SLEEP         = 0.5
NOTION_SLEEP           = 0.35
FETCH_BONUS_MULTIPLIER = 2     # bonus fetch multiplier untuk seed aktif
HIGH_YIELD_THRESHOLD   = 0.5   # ratio new/fetched >= ini → seed dianggap "aktif"

# File lokal untuk dedup — persistent lintas run
REJECTED_FILE  = "/root/.openclaw/workspace/NFTtwitterbot/NFTtwitterbot/runtime/sniper_rejected.txt"  # kena filter gate
SEEN_FILE      = "/root/.openclaw/workspace/NFTtwitterbot/NFTtwitterbot/runtime/sniper_seen.txt"      # sudah di Notion

# ── Loaders ───────────────────────────────────────────────────────────────────

def load_sessions() -> list[dict]:
    sessions = []
    for fpath in COOKIE_FILES:
        p = Path(fpath)
        if not p.exists():
            continue
        count_before = len(sessions)
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts: dict[str, str] = {}
            for chunk in line.split(";"):
                chunk = chunk.strip()
                if "=" in chunk:
                    k, v = chunk.split("=", 1)
                    parts[k.strip()] = v.strip()
            if "auth_token" in parts and "ct0" in parts:
                sessions.append({
                    "cookies": {"auth_token": parts["auth_token"], "ct0": parts["ct0"]},
                    "ct0":     parts["ct0"],
                    "uid":     parts.get("twid", "").replace("u%3D", ""),
                    "source":  p.name,
                })
        log.info("  %d sessions from %s", len(sessions) - count_before, p.name)
    log.info("Total sessions: %d", len(sessions))
    return sessions


def load_proxies() -> list[dict]:
    p = Path(STATIC_PROXY_FILE)
    if p.exists():
        proxies = []
        for line in p.read_text().splitlines():
            parts = line.strip().split(":")
            if len(parts) == 4:
                ip, port, user, pwd = parts
                url = f"http://{user}:{pwd}@{ip}:{port}"
                proxies.append({"http": url, "https": url})
        if proxies:
            return proxies
    rp = Path(ROTATING_PROXY_FILE)
    if rp.exists():
        u = rp.read_text().strip()
        return [{"http": u, "https": u}]
    return []


def load_ct_list() -> list[str]:
    handles = list(BASE_CT_LIST)
    seen = {h.lower() for h in handles}
    p = Path(CT_NFT_FILE)
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            # Accept @handle, plain handle, or x/twitter URL only. Ignore docs/comments.
            m = re.search(r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,20})", raw)
            h = m.group(1) if m else raw.lstrip("@").strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{1,20}", h):
                continue
            if h.lower() in {"handle", "username", "twitter", "example"}:
                continue
            if h.lower() not in seen:
                handles.append(h)
                seen.add(h.lower())
    return handles


def load_state() -> dict:
    p = Path(STATE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"seeds": {}}


def save_state(state: dict):
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))


def load_set(fpath: str) -> set[str]:
    """Load satu handle per baris dari file ke set (lowercase)."""
    p = Path(fpath)
    if not p.exists():
        return set()
    return {line.strip().lower() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}


def append_to_set_file(fpath: str, handles: list[str]):
    """Tambah handles baru ke file (no-duplicate, sorted)."""
    existing = load_set(fpath)
    new_ones = [h.lower() for h in handles if h.lower() not in existing]
    if not new_ones:
        return
    with open(fpath, "a", encoding="utf-8") as f:
        for h in new_ones:
            f.write(h + "\n")

# ── Twitter ───────────────────────────────────────────────────────────────────

def build_headers(s: dict) -> dict:
    return {
        "Authorization":         f"Bearer {BEARER}",
        "x-csrf-token":          s["ct0"],
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type":   "OAuth2Session",
        "User-Agent":            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept":                "application/json",
        "Referer":               "https://twitter.com/",
    }


def validate_session(s: dict, prx: dict | None) -> bool:
    try:
        r = requests.get(
            "https://twitter.com/i/api/graphql/NimuplG1OB7Fd2btCLdBOw/UserByScreenName",
            params={
                "variables": json.dumps({"screen_name": "twitter", "withSafetyModeUserFields": False}),
                "features":  json.dumps({"responsive_web_graphql_timeline_navigation_enabled": True}),
            },
            headers=build_headers(s), cookies=s["cookies"], proxies=prx, timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def get_recent_following(screen_name: str, s: dict, prx: dict | None,
                         count: int = FETCH_PER_SEED) -> list[dict]:
    """
    Ambil N following terbaru dari seed (reverse-chronological, terbaru dulu).
    Default count = FETCH_PER_SEED (15), bisa override.
    """
    try:
        r = requests.get(
            "https://api.twitter.com/1.1/friends/list.json",
            params={
                "screen_name":           screen_name,
                "count":                 count,
                "skip_status":           True,
                "include_user_entities": False,
            },
            headers=build_headers(s), cookies=s["cookies"], proxies=prx, timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("users", [])
        log.warning("  [%d] @%s: %s", r.status_code, screen_name, r.text[:80])
    except Exception as e:
        log.error("  get_recent_following @%s: %s", screen_name, e)
    return []

# ── Account Classifier ────────────────────────────────────────────────────────
#
# classify_account() mengembalikan salah satu dari:
#   "nft"          → CT NFT: project / collector / onchain artist → main Notion
#   "ct_only"      → CT tapi bukan NFT (trader, defi, web3 builder) → reject Notion
#   "hard_reject"  → arabic / airdrop / bio kosong → silent, file dedup saja
#   "not_ct"       → sama sekali bukan crypto → silent, file dedup saja
#
# Routing:
#   hard_reject  → sniper_rejected.txt
#   not_ct       → sniper_rejected.txt
#   ct_only      → reject Notion DB + sniper_rejected.txt
#   nft          → main Notion DB + sniper_seen.txt

_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")

# Signal NFT/onchain art spesifik — satu cukup untuk lolos ke main Notion
_NFT_SIGNAL = {
    "nft", "nfts", "pfp", "mint", "minting", "holder", "hodler",
    "opensea", "superrare", "foundation", "manifold", "zora",
    "ordinals", "onchain", "on chain", "on-chain",
    "generative", "digital art", "crypto art", "web3 art",
    "jpeg", "collection", "collector",
    "discord.gg",       # NFT project selalu punya discord
}

# Location = nama chain → project NFT sering set ini (bukan manusia biasa)
_CHAIN_LOCATION = {
    "eth", "ethereum", "base", "solana", "sol", "btc", "bitcoin",
    "polygon", "matic", "arbitrum", "optimism", "avalanche", "avax",
    "on-chain", "on chain", "onchain",
}


_PROJECT_NFT_SIGNAL = {
    "collection", "collections", "generative", "digital art", "crypto art", "web3 art",
    "onchain art", "on-chain art", "pfp", "minting", "mint page", "opensea",
    "superrare", "foundation", "manifold", "zora", "ordinals", "discord.gg",
    "coming soon", "launching", "launched", "genesis", "allowlist", "artist collective",
}

_INDIVIDUAL_NOISE = {
    "collector", "holder", "hodler", "trader", "investor", "degen", "alpha", "gem",
    "nfa", "dyor", "memes", "reply guy", "shitposter", "community", "ambassador",
    "founder", "co-founder", "ceo", "marketing", "growth", "bd", "student",
}

_PROJECT_NAME_SUFFIX = ("nft", "art", "arts", "club", "collective", "studio", "labs", "dao", "xyz")

def project_nft_signal_score(name: str, bio: str, location: str) -> int:
    combined = (name + " " + bio + " " + location).lower()
    score = 0
    score += sum(1 for kw in _PROJECT_NFT_SIGNAL if kw in combined)
    if any(name.lower().replace("_", "").endswith(s) for s in _PROJECT_NAME_SUFFIX):
        score += 1
    if "discord.gg" in combined or "opensea" in combined or "manifold" in combined or "zora" in combined:
        score += 1
    # Chain location alone is weak, not auto-pass.
    if location in _CHAIN_LOCATION and any(kw in combined for kw in ["nft", "art", "collection", "mint", "pfp", "ordinal"]):
        score += 1
    return score

def individual_noise_score_nft(name: str, bio: str) -> int:
    combined = (name + " " + bio).lower()
    return sum(1 for kw in _INDIVIDUAL_NOISE if kw in combined)

# Signal CT umum — cukup untuk dimasukkan ke reject Notion (bukan silent reject)
# CT generic bio: "degen trader", "building in web3", "eth maxi", "crypto native", dll
_CT_SIGNAL = {
    # Slang / culture
    "degen", "lfg", "wagmi", "ngmi", "probably nothing", "gm", "fren",
    "ape", "rekt", "alpha", "based", "hodl", "buidl", "ngmi", "ser",
    # Crypto identity
    "crypto", "web3", "blockchain", "defi", "eth", "btc", "sol", "base",
    # Activity
    "trading", "trader", "investor", "yield", "staking", "dao",
    "protocol", "liquidity", "portfolio", "airdrop hunter",
    # Ecosystem
    "ethereum", "solana", "bitcoin", "polygon", "arbitrum",
    "optimism", "avalanche", "hyperliquid", "uniswap",
    # NFT signals are superset of CT (so ct_only check comes AFTER nft check)
}

_AIRDROP_KW = {
    "airdrop", "free drop", "claim", "giveaway", "freemint", "free mint",
    "whitelist", " wl ", "alpha call", "alpha group",
}


def classify_account(user: dict) -> str:
    """
    Return: "nft" | "ct_only" | "hard_reject" | "not_ct"

    Tightened 2026-05-24 after smoke test polluted Main with individual CT.
    Main DB should be NFT project / onchain artist / collection-like accounts,
    not generic collectors, traders, reply-guys, or individual CT.
    """
    followers = user.get("followers_count") or 0
    if followers > FOLLOWERS_CAP:
        return "hard_reject"

    name     = (user.get("name") or "").lower()
    handle   = (user.get("screen_name") or user.get("screenName") or "").lower()
    bio      = (user.get("description") or "").strip()
    bio_low  = bio.lower()
    location = (user.get("location") or "").strip().lower()
    combined = name + " " + handle + " " + bio_low

    if _ARABIC_RE.search(name + bio_low):
        return "hard_reject"
    if any(kw in combined for kw in _AIRDROP_KW):
        return "hard_reject"
    if len(bio) < 8:
        # Thin bios only pass if handle/name is clearly project-shaped NFT.
        return "nft" if project_nft_signal_score(handle or name, bio_low, location) >= 2 else "hard_reject"

    has_ct = any(kw in combined for kw in _CT_SIGNAL)
    has_nft_keyword = any(kw in combined for kw in _NFT_SIGNAL)
    project_score = project_nft_signal_score(handle or name, bio_low, location)
    individual_score = individual_noise_score_nft(name + " " + handle, bio_low)

    # Do NOT let one weak word like collector/holder/mint/onchain/location auto-pass.
    if has_nft_keyword and project_score >= 2 and individual_score <= 1:
        return "nft"
    if has_nft_keyword and project_score >= 3:
        return "nft"

    if has_ct or has_nft_keyword:
        return "ct_only"
    return "not_ct"

# ── Alpha Score → Tier ────────────────────────────────────────────────────────
#
#  Smart (frontrun)        : +40
#  Followers  100–1k → +10 | <100 → +5   (cap 1k jadi >1k tidak mungkin masuk)
#  NFT keyword di bio      : +10
#  Account age ≥3yr → +20 | ≥1yr → +10
#
#  Tier: A ≥ 70 | B ≥ 40 | C < 40

def alpha_score(user: dict, smart: bool) -> int:
    score = 40 if smart else 0

    followers = user.get("followers_count", 0)
    if followers > 100:
        score += 10
    else:
        score += 5

    if any(kw in (user.get("description") or "").lower() for kw in _NFT_SIGNAL):
        score += 10

    if user.get("created_at"):
        try:
            age = (datetime.datetime.utcnow() -
                   datetime.datetime.strptime(user["created_at"], "%a %b %d %H:%M:%S +0000 %Y")).days
            score += 20 if age >= 3 * 365 else (10 if age >= 365 else 0)
        except Exception:
            pass

    return score


def tier_from_score(score: int) -> str:
    return "A" if score >= 70 else ("B" if score >= 40 else "C")

# ── Frontrun ──────────────────────────────────────────────────────────────────

def check_smart(username: str) -> bool:
    try:
        r = requests.get(
            f"{FRONTRUN_BASE}/api/v3/twitter/{username}/info",
            headers={
                "Content-Type":           "application/json",
                "copilot-client-version": "0.0.221",
                "User-Agent":             "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
                "Cookie": (f"__Secure-frontrun.session_token={FRONTRUN_TOKEN}; "
                           "__Secure-frontrun.session_token_domain_migrate=1"),
                "Accept": "application/json",
            },
            timeout=10,
        )
        if r.status_code == 200:
            info = r.json().get("data") or {}
            return (any(w.get("verified") for w in info.get("wallets", []))
                    or bool(info.get("position") or info.get("primaryLabel") or info.get("compactLabel")))
    except Exception:
        pass
    return False

# ── Notion ────────────────────────────────────────────────────────────────────

def _notion_headers() -> dict:
    return {
        "Authorization":  f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type":   "application/json",
    }


def _reject_notion_headers() -> dict:
    return {
        "Authorization":  f"Bearer {REJECT_NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type":   "application/json",
    }


def upsert_reject_notion(user: dict, source_seed: str, reason: str) -> bool:
    """Kirim akun CT-tapi-bukan-NFT ke reject Notion DB untuk manual review."""
    handle    = user["screen_name"]
    followers = user.get("followers_count", 0)
    bio       = (user.get("description") or "")[:2000]
    today     = datetime.date.today().isoformat()

    account_created = None
    if user.get("created_at"):
        try:
            dt = datetime.datetime.strptime(user["created_at"], "%a %b %d %H:%M:%S +0000 %Y")
            account_created = dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    props = {
        "Handle":          {"title":     [{"text": {"content": handle}}]},
        "X Link":          {"url":       f"https://x.com/{handle}"},
        "Bio":             {"rich_text": [{"text": {"content": bio}}] if bio else []},
        "Followers":       {"number":    followers},
        "Last Added":      {"date":      {"start": today}},
        "Source":          {"rich_text": [{"text": {"content": f"following:@{source_seed}"}}]},
        "Reason":          {"rich_text": [{"text": {"content": reason}}]},
        "Recent Username": {"rich_text": [{"text": {"content": handle}}]},
    }
    if account_created:
        props["Account Created"] = {"date": {"start": account_created}}

    try:
        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers=_reject_notion_headers(),
            json={"parent": {"database_id": REJECT_NOTION_DB_ID}, "properties": props},
            timeout=20,
        )
        if r.status_code == 200:
            log.info("  [RejectDB] @%-20s  reason=%s", handle, reason)
            return True
        log.warning("  [RejectDB] @%s HTTP %d: %s", handle, r.status_code, r.text[:100])
    except Exception as e:
        log.error("  [RejectDB] @%s error: %s", handle, e)
    return False


def get_existing_handles() -> dict[str, str]:
    existing: dict[str, str] = {}
    cursor = None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        r = requests.post(f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
                          headers=_notion_headers(), json=payload, timeout=15)
        data = r.json()
        for row in data.get("results", []):
            if row.get("archived"):
                continue
            url = (row["properties"].get("Link") or {}).get("url") or ""
            h = url.rstrip("/").split("/")[-1].lstrip("@").lower()
            if h:
                existing[h] = row["id"]
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return existing


def upsert_notion(user: dict, tier: str, score: int, smart: bool,
                  source_seed: str, cache: dict[str, str]) -> bool:
    handle    = user["screen_name"]
    name      = user.get("name") or handle
    followers = user.get("followers_count", 0)
    bio       = (user.get("description") or "")[:2000]
    today     = datetime.date.today().isoformat()

    account_created = None
    if user.get("created_at"):
        try:
            dt = datetime.datetime.strptime(user["created_at"], "%a %b %d %H:%M:%S +0000 %Y")
            account_created = dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    page_id = cache.get(handle.lower())

    if page_id:
        verb  = "Updated"
        props = {
            "Last Checked":  {"date":     {"start": today}},
            "Tier":          {"select":   {"name": tier}},
            "Alpha Score":   {"number":   score},
            "Smart account": {"checkbox": smart},
            "Follower":      {"number":   followers},
        }
        call = lambda: requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=_notion_headers(), json={"properties": props}, timeout=20,
        )
    else:
        verb  = "Created"
        props = {
            "Akun":          {"title":     [{"text": {"content": name}}]},
            "Link":          {"url":       f"https://x.com/{handle}"},
            "Follower":      {"number":    followers},
            "Bio":           {"rich_text": [{"text": {"content": bio}}] if bio else []},
            "Smart account": {"checkbox":  smart},
            "Tier":          {"select":    {"name": tier}},
            "Category":      {"select":    {"name": "NFT"}},
            "Added date":    {"date":      {"start": today}},
            "Last Checked":  {"date":      {"start": today}},
            "Alpha Score":   {"number":    score},
            "Source Seed":   {"rich_text": [{"text": {"content": f"following:@{source_seed}"}}]},
        }
        if account_created:
            props["Account Created"] = {"date": {"start": account_created}}
        call = lambda: requests.post(
            "https://api.notion.com/v1/pages",
            headers=_notion_headers(),
            json={"parent": {"database_id": NOTION_DB_ID}, "properties": props},
            timeout=20,
        )

    for attempt in range(3):
        try:
            r = call()
            break
        except requests.exceptions.Timeout:
            log.warning("  Notion timeout @%s (attempt %d/3)", handle, attempt + 1)
            time.sleep(3)
        except Exception as e:
            log.error("  Notion error @%s: %s", handle, e)
            return False
    else:
        log.error("  Notion @%s failed after 3 retries", handle)
        return False

    if r.status_code in (200, 201):
        if verb == "Created":
            cache[handle.lower()] = r.json()["id"]
        log.info("  [%s] @%-22s tier=%s score=%3d smart=%s followers=%d",
                 verb, handle, tier, score, smart, followers)
        return True
    log.error("  Notion %s @%s HTTP %d: %s", verb, handle, r.status_code, r.text[:150])
    return False

# ── Session pool (round-robin) ────────────────────────────────────────────────

class SessionPool:
    def __init__(self, sessions: list[dict], proxies: list[dict]):
        self._sessions = sessions
        self._proxies  = proxies
        self._idx      = 0

    def next(self) -> dict:
        s = self._sessions[self._idx]
        if not s.get("proxy") and self._proxies:
            s["proxy"] = self._proxies[self._idx % len(self._proxies)]
        self._idx = (self._idx + 1) % len(self._sessions)
        return s

# ── One monitoring cycle ──────────────────────────────────────────────────────

def run_cycle(pool: SessionPool, ct_seeds: list[str], state: dict,
              notion_cache: dict[str, str], is_first_run: bool,
              rejected_set: set[str], seen_set: set[str]) -> dict:
    stats = {
        "new_follows": 0, "added_notion": 0, "added_reject_db": 0,
        "skip_rejected": 0, "skip_seen": 0,
        "hard_reject": 0, "not_ct": 0, "ct_only": 0,
    }
    seeds_state   = state.setdefault("seeds", {})
    new_rejected: list[str] = []
    new_seen:     list[str] = []

    # Adaptive: proses seed paling produktif duluan (highest last_yield first)
    sorted_seeds = sorted(
        ct_seeds,
        key=lambda h: seeds_state.get(h.lower(), {}).get("last_yield", 0),
        reverse=True,
    )

    for seed_handle in sorted_seeds:
        seed_key   = seed_handle.lower()
        seed_info  = seeds_state.get(seed_key, {})
        known_ids  = set(seed_info.get("known_ids", []))
        first_time = len(known_ids) == 0

        # Adaptive fetch budget: bonus untuk seed yang aktif di cycle sebelumnya
        prev_yield   = seed_info.get("last_yield", 0)
        prev_fetched = seed_info.get("last_fetched", FETCH_PER_SEED)
        is_active    = (not first_time
                        and prev_fetched > 0
                        and prev_yield / prev_fetched >= HIGH_YIELD_THRESHOLD)
        fetch_count  = FETCH_PER_SEED * FETCH_BONUS_MULTIPLIER if is_active else FETCH_PER_SEED

        s     = pool.next()
        users = get_recent_following(seed_handle, s, s.get("proxy"), count=fetch_count)

        if not users:
            log.info("  @%-20s → no data", seed_handle)
            continue

        current_ids = [str(u["id"]) for u in users]
        current_set = set(current_ids)

        # Diff: hanya yang baru muncul sejak run terakhir
        new_user_ids = current_set - known_ids
        targets      = users if first_time else [u for u in users if str(u["id"]) in new_user_ids]

        # Yield untuk adaptive: 0 saat baseline (belum ada data real diff)
        new_yield = 0 if first_time else len(targets)
        yield_pct = (new_yield / fetch_count * 100) if (not first_time and fetch_count) else 0
        tag       = "[BASELINE]" if first_time else ("[ACTIVE]" if is_active else "[STALE]")
        log.info("  @%-20s → %d/%d %s  yield=%.0f%%  %s",
                 seed_handle, len(targets), fetch_count,
                 "baseline" if first_time else "new",
                 yield_pct, tag)

        # Simpan state: snapshot + yield data untuk adaptive next cycle
        seeds_state[seed_key] = {
            "known_ids":    current_ids,
            "last_check":   datetime.datetime.now().isoformat(),
            "last_yield":   new_yield,
            "last_fetched": fetch_count,
        }
        save_state(state)

        if not targets:
            continue

        stats["new_follows"] += len(targets)

        for user in targets:
            handle = user["screen_name"].lower()

            # Gate 1: sudah di Notion → skip total
            if handle in seen_set or handle in notion_cache:
                stats["skip_seen"] += 1
                if handle not in seen_set:
                    new_seen.append(handle)
                continue

            # Gate 2: pernah di-reject → skip total
            if handle in rejected_set:
                stats["skip_rejected"] += 1
                continue

            # Gate 3: classify account
            label = classify_account(user)

            if label == "hard_reject" or label == "not_ct":
                # Silent reject — file dedup saja, tidak perlu masuk Notion manapun
                stats["hard_reject" if label == "hard_reject" else "not_ct"] += 1
                new_rejected.append(handle)
                log.debug("    [%s] @%s", label.upper(), user["screen_name"])
                continue

            if label == "ct_only":
                # CT tapi bukan NFT → reject Notion DB untuk review
                stats["ct_only"] += 1
                new_rejected.append(handle)
                upsert_reject_notion(user, seed_handle, "ct_not_nft")
                time.sleep(NOTION_SLEEP)
                continue

            # label == "nft" → lolos semua gate, proses ke main Notion
            smart = check_smart(user["screen_name"])
            time.sleep(FRONTRUN_SLEEP)

            score = alpha_score(user, smart)
            tier  = tier_from_score(score)

            ok = upsert_notion(user, tier, score, smart, seed_handle, notion_cache)
            if ok:
                stats["added_notion"] += 1
                new_seen.append(handle)
            time.sleep(NOTION_SLEEP)

    # Persist rejected & seen ke file
    if new_rejected:
        append_to_set_file(REJECTED_FILE, new_rejected)
        rejected_set.update(new_rejected)
    if new_seen:
        append_to_set_file(SEEN_FILE, new_seen)
        seen_set.update(new_seen)

    return stats

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop",     action="store_true", help="Jalankan terus menerus")
    parser.add_argument("--interval", type=int, default=INTERVAL_MIN,
                        help=f"Interval antar run dalam menit (default: {INTERVAL_MIN})")
    args = parser.parse_args()

    log.info("=== NFT Sniper X — %s | interval=%dm ===",
             "LOOP" if args.loop else "ONCE", args.interval)

    sessions = load_sessions()
    proxies  = load_proxies()
    ct_seeds = load_ct_list()
    log.info("CT seeds: %d | Proxies: %d", len(ct_seeds), len(proxies))

    log.info("Validating sessions...")
    live: list[dict] = []
    for i, s in enumerate(sessions):
        prx = proxies[i % len(proxies)] if proxies else None
        if validate_session(s, prx):
            s["proxy"] = prx
            live.append(s)
            log.info("  [ALIVE] uid=%s (%s)", s.get("uid", "?")[:20], s["source"])
        else:
            log.warning("  [DEAD]  uid=%s (%s)", s.get("uid", "?")[:20], s["source"])
    log.info("Live: %d/%d\n", len(live), len(sessions))

    if not live:
        log.error("No live sessions. Exiting.")
        return

    pool  = SessionPool(live, proxies)
    state = load_state()
    is_first_run = len(state.get("seeds", {})) == 0

    # Load persistent dedup sets
    rejected_set = load_set(REJECTED_FILE)
    seen_set     = load_set(SEEN_FILE)
    log.info("Dedup: rejected=%d  seen=%d", len(rejected_set), len(seen_set))

    cycle = 0
    while True:
        cycle += 1
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        log.info("=== CYCLE %d — %s%s ===",
                 cycle, now, " [BASELINE]" if is_first_run else "")

        notion_cache = get_existing_handles()
        log.info("Notion: %d entries | base=%d/seed  bonus=%dx for active seeds",
                 len(notion_cache), FETCH_PER_SEED, FETCH_BONUS_MULTIPLIER)

        stats = run_cycle(pool, ct_seeds, state, notion_cache,
                          is_first_run, rejected_set, seen_set)
        is_first_run = False

        log.info("── Cycle %d: new=%d | main=%d reject_db=%d | skip_seen=%d skip_rej=%d | hard=%d not_ct=%d ct_only=%d",
                 cycle, stats["new_follows"],
                 stats["added_notion"], stats["added_reject_db"],
                 stats["skip_seen"], stats["skip_rejected"],
                 stats["hard_reject"], stats["not_ct"], stats["ct_only"])

        if not args.loop:
            break

        next_run = datetime.datetime.now() + datetime.timedelta(minutes=args.interval)
        log.info("Next check in %dm — sleeping until %s\n",
                 args.interval, next_run.strftime("%H:%M"))
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
