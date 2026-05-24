#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_PATH = Path('/root/.openclaw/workspace/memory/alpha_account_state.json')
SCOUT_PATH = '/root/.openclaw/workspace/skills/alpha_account_scout/scripts/scout.py'
TARGET_NEW = int(os.environ.get('ALPHA_TARGET_NEW', '25'))
MAX_PASSES = int(os.environ.get('ALPHA_MAX_PASSES', '24'))
STALL_LIMIT = int(os.environ.get('ALPHA_STALL_LIMIT', '6'))
SLEEP_SECONDS = float(os.environ.get('ALPHA_PASS_SLEEP_SECONDS', '8'))


def count_handles():
    if not STATE_PATH.exists():
        return 0
    try:
        data = json.loads(STATE_PATH.read_text())
        return len((data.get('handles') or {}))
    except Exception:
        return 0


def main():
    baseline = count_handles()
    target_total = baseline + TARGET_NEW
    current = baseline
    stalled = 0

    print(f'[until-target] baseline={baseline} target_new={TARGET_NEW} target_total={target_total}')

    for attempt in range(1, MAX_PASSES + 1):
        if current >= target_total:
            break

        print(f'[until-target] pass {attempt}/{MAX_PASSES} starting (current={current}, remaining={target_total-current})')
        proc = subprocess.run([sys.executable, SCOUT_PATH], text=True)
        if proc.returncode != 0:
            print(f'[until-target] scout exited non-zero: {proc.returncode}')

        new_current = count_handles()
        gained = new_current - current
        total_gained = new_current - baseline
        print(f'[until-target] pass {attempt} result: gained={gained}, total_gained={total_gained}, current={new_current}')

        if new_current >= target_total:
            current = new_current
            break

        if gained <= 0:
            stalled += 1
        else:
            stalled = 0
        current = new_current

        if stalled >= STALL_LIMIT:
            print(f'[until-target] stopping after {stalled} stalled passes without new accounts')
            break

        time.sleep(SLEEP_SECONDS)

    final_count = count_handles()
    added = final_count - baseline
    print(f'[until-target] finished: added={added}, final_total={final_count}, target_new={TARGET_NEW}, success={final_count >= target_total}')


if __name__ == '__main__':
    main()
