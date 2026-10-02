#!/usr/bin/env python3
"""Staleness alarm for the vault's git backup.

A failure mode this guards against: the backup repo froze for 4 days and everything downstream
(any consumer of the backup) silently served stale data. This raises the alarm.

ALARM (exit 1, state/STALE flag file with JSON reasons) when ANY of:
  * committer heartbeat missing or older than HEARTBEAT_MAX_S (default 900 s): the committer is not running
  * HEAD commit older than STALE_AFTER_S (default 6 h) while the tree is dirty
  * a remote is configured and there are unpushed commits whose oldest is older than STALE_AFTER_S
    (or the last successful push is older than that while HEAD is ahead of it)
  * the repo is unreadable / has no commits while the vault has files
Exit 0 and remove the flag otherwise. Usage: staleness.py [--no-flag] [--json]
"""
import json
import os
import subprocess
import sys
import time


class Cfg:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.repo = e.get("VAULT_ROOT", "/vault")
        self.state = e.get("STATE_DIR", "/state")
        self.stale_after = float(e.get("STALE_AFTER_S", str(6 * 3600)))
        self.hb_max = float(e.get("HEARTBEAT_MAX_S", "900"))
        self.remote = e.get("COMMITTER_REMOTE", "").strip()


def _git(cfg, *args):
    return subprocess.run(["git", "-c", "safe.directory=*", "-c", "core.filemode=false", "-C", cfg.repo, *args],
                          capture_output=True, text=True, timeout=60)


def evaluate(cfg, now=None):
    now = now or time.time()
    reasons = []
    try:
        hb = float(open(os.path.join(cfg.state, "committer.heartbeat")).read().strip())
        if now - hb > cfg.hb_max:
            reasons.append(f"committer heartbeat is {int(now - hb)}s old (max {int(cfg.hb_max)}s): committer not running")
    except (OSError, ValueError):
        reasons.append("committer heartbeat missing: committer has never run")

    head_ts = None
    p = _git(cfg, "log", "-1", "--format=%ct")
    if p.returncode == 0 and p.stdout.strip():
        head_ts = float(p.stdout.strip())
    st = _git(cfg, "status", "--porcelain", "--untracked-files=all")
    if st.returncode != 0:
        reasons.append(f"git status failed: {st.stderr.strip()[:200]}")
        dirty = []
    else:
        dirty = [l for l in st.stdout.splitlines() if l.strip()]
    if head_ts is None:
        if dirty:
            reasons.append("repo has no commits but the vault has files")
    elif dirty and now - head_ts > cfg.stale_after:
        reasons.append(f"HEAD is {int((now - head_ts) / 60)} min old and {len(dirty)} path(s) are dirty")

    if cfg.remote and head_ts is not None:
        pushed_f = os.path.join(cfg.state, "last_pushed")
        try:
            last_pushed = open(pushed_f).read().strip()
        except OSError:
            last_pushed = ""
        head = _git(cfg, "rev-parse", "HEAD").stdout.strip()
        if last_pushed != head:
            try:
                lp = float(open(os.path.join(cfg.state, "last_push_time")).read().strip())
            except (OSError, ValueError):
                lp = None
            if lp is None:
                # never pushed: measure from the HEAD commit time
                if now - head_ts > cfg.stale_after:
                    reasons.append("backup remote has never received HEAD and HEAD is older than the limit")
            elif now - lp > cfg.stale_after:
                reasons.append(f"last successful push was {int((now - lp) / 60)} min ago and HEAD is ahead of the remote")
    return reasons


def run(cfg, write_flag=True, now=None):
    reasons = evaluate(cfg, now)
    flag = os.path.join(cfg.state, "STALE")
    if write_flag:
        os.makedirs(cfg.state, exist_ok=True)
        if reasons:
            tmp = flag + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"since": int(now or time.time()), "reasons": reasons}, fh)
            os.replace(tmp, flag)
        else:
            try:
                os.unlink(flag)
            except OSError:
                pass
    return reasons


def main(argv):
    reasons = run(Cfg(), write_flag="--no-flag" not in argv)
    if reasons:
        sys.stderr.write("VAULT BACKUP STALE:\n" + "".join(f"  - {r}\n" for r in reasons))
        return 1
    if "--json" in argv:
        print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
