#!/usr/bin/env python3
"""vault committer: debounced `git add -A && git commit` of the vault, optional push.

Basic Memory never commits, so this sidecar is the ONLY thing that makes the vault's
git history (and backup remote) current. Loop (default every 60 s):
  1. write heartbeat (state/committer.heartbeat) -- the staleness alarm reads it
  2. if the tree is dirty AND the newest dirty file is at least DEBOUNCE_S old
     (writers are done), commit with message 'vault: auto <UTC timestamp>'
  3. if COMMITTER_REMOTE is set, push HEAD to it when HEAD differs from the last pushed
     commit (failures are logged, state kept, retried next cycle)
  4. run the staleness check (writes/clears state/STALE)

Details that matter on Unraid/SMB: core.filemode=false (SMB mode-bit noise),
git identity + safe.directory passed with -c (no global config, container user has no
home), .git/index.lock handled (waited out, removed only if older than STALE_LOCK_S
and no git process owns it), shim temp files (.*.tmp) excluded via .git/info/exclude.
Stdlib only.
"""
import json
import os
import subprocess
import sys
import time

GIT_ID = ["-c", "user.name=vault-committer", "-c", "user.email=vault-committer@localhost",
          "-c", "safe.directory=*", "-c", "core.filemode=false", "-c", "commit.gpgsign=false"]
EXCLUDES = [".*.tmp", ".DS_Store", "Thumbs.db", "*.swp", ".obsidian/workspace*.json"]


class Cfg:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.repo = e.get("VAULT_ROOT", "/vault")
        self.state = e.get("STATE_DIR", "/state")
        self.interval = float(e.get("COMMIT_INTERVAL_S", "60"))
        self.debounce = float(e.get("COMMIT_DEBOUNCE_S", "15"))
        self.remote = e.get("COMMITTER_REMOTE", "").strip()
        self.branch = e.get("COMMITTER_BRANCH", "main")
        self.stale_lock_s = float(e.get("STALE_LOCK_S", "600"))
        self.lock_wait_s = float(e.get("LOCK_WAIT_S", "30"))
        self.init_repo = e.get("COMMITTER_INIT", "1") == "1"
        self.git_timeout = float(e.get("GIT_TIMEOUT_S", "120"))
        self.push_timeout = float(e.get("PUSH_TIMEOUT_S", "90"))


def log(msg):
    sys.stderr.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} committer: {msg}\n")
    sys.stderr.flush()


def git(cfg, *args, check=True, timeout=None):
    p = subprocess.run(["git", *GIT_ID, "-C", cfg.repo, *args], capture_output=True, text=True,
                       timeout=timeout or cfg.git_timeout,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {p.returncode}: {p.stderr.strip()[:300]}")
    return p


def ensure_repo(cfg):
    if not os.path.isdir(os.path.join(cfg.repo, ".git")):
        if not cfg.init_repo:
            raise RuntimeError(f"{cfg.repo} is not a git repo and COMMITTER_INIT=0")
        git(cfg, "init", "-q", "-b", cfg.branch)
        log(f"initialised git repo in {cfg.repo}")
    git(cfg, "config", "core.filemode", "false")
    git(cfg, "config", "core.autocrlf", "false")
    excl = os.path.join(cfg.repo, ".git", "info", "exclude")
    os.makedirs(os.path.dirname(excl), exist_ok=True)
    have = open(excl).read().splitlines() if os.path.exists(excl) else []
    missing = [x for x in EXCLUDES if x not in have]
    if missing:
        with open(excl, "a") as fh:
            fh.write("\n".join(missing) + "\n")
    if cfg.remote:
        try:
            ensure_remote(cfg)
        except Exception as e:  # an unusable backup must not stop local commits (or loop forever)
            log(f"WARNING: backup remote {cfg.remote} unusable ({e}); continuing with LOCAL commits only, "
                f"no push. Fix: make the backup dir writable by uid {os.getuid()} (e.g. chown 99:100) and restart the container")
            cfg.remote = ""
            os.environ["COMMITTER_REMOTE"] = ""  # staleness alarm must not expect pushes either


def ensure_remote(cfg):
    if not cfg.remote.startswith(("/", "./")) and "://" not in cfg.remote and "@" not in cfg.remote:
        raise RuntimeError(f"COMMITTER_REMOTE {cfg.remote!r} looks invalid")
    if cfg.remote.startswith("/") and not os.path.exists(cfg.remote):
        os.makedirs(os.path.dirname(cfg.remote), exist_ok=True)
        p = subprocess.run(["git", "init", "-q", "--bare", "-b", cfg.branch, cfg.remote],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"git init --bare {cfg.remote} -> {p.returncode}: {p.stderr.strip()[:300]}")
        log(f"created bare backup repo {cfg.remote}")
    cur = git(cfg, "remote", "get-url", "backup", check=False)
    if cur.returncode != 0:
        git(cfg, "remote", "add", "backup", cfg.remote)
    elif cur.stdout.strip() != cfg.remote:
        git(cfg, "remote", "set-url", "backup", cfg.remote)


def dirty_files(cfg):
    out = git(cfg, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    files = []
    for ent in out.split("\0"):
        if len(ent) > 3:
            files.append(ent[3:])
    return files


def newest_mtime(cfg, files):
    """Newest mtime among dirty paths. A deleted path has no mtime, so use its nearest
    existing parent directory (unlink bumps the dir mtime)."""
    newest = 0.0
    for f in files:
        p = os.path.join(cfg.repo, f)
        while True:
            try:
                newest = max(newest, os.stat(p).st_mtime)
                break
            except OSError:
                if os.path.normpath(p) == os.path.normpath(cfg.repo):
                    break
                p = os.path.dirname(p)
    return newest


def handle_index_lock(cfg):
    """Wait out a live lock; remove it only if stale and no git process is running."""
    lock = os.path.join(cfg.repo, ".git", "index.lock")
    deadline = time.time() + cfg.lock_wait_s
    while os.path.exists(lock):
        try:
            age = time.time() - os.stat(lock).st_mtime
        except OSError:
            return True
        if age > cfg.stale_lock_s and not _git_running():
            log(f"removing stale index.lock (age {age:.0f}s)")
            try:
                os.unlink(lock)
            except OSError:
                pass
            return True
        if time.time() > deadline:
            log(f"index.lock present (age {age:.0f}s), will retry next cycle")
            return False
        time.sleep(0.5)
    return True


def _git_running():
    try:
        r = subprocess.run(["pgrep", "-x", "git"], capture_output=True)
        return r.returncode == 0
    except OSError:
        return False


def commit_if_dirty(cfg, now=None):
    """Returns 'clean' | 'debounce' | 'locked' | 'committed' | 'error'."""
    files = dirty_files(cfg)
    if not files:
        return "clean"
    now = now or time.time()
    if now - newest_mtime(cfg, files) < cfg.debounce:
        return "debounce"
    if not handle_index_lock(cfg):
        return "locked"
    for attempt in range(3):
        try:
            git(cfg, "add", "-A")
            msg = "vault: auto " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            p = git(cfg, "commit", "-q", "-m", msg, "--no-verify", check=False)
            if p.returncode == 0:
                log(f"committed {len(files)} changed path(s)")
                return "committed"
            if "nothing to commit" in (p.stdout + p.stderr):
                return "clean"
            if "index.lock" in p.stderr:
                time.sleep(1 + attempt)
                handle_index_lock(cfg)
                continue
            log(f"commit failed: {p.stderr.strip()[:300]}")
            return "error"
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            if "index.lock" in str(e):
                time.sleep(1 + attempt)
                handle_index_lock(cfg)
                continue
            log(f"git error: {e}")
            return "error"
    return "locked"


def head_sha(cfg):
    p = git(cfg, "rev-parse", "-q", "--verify", "HEAD", check=False)
    return p.stdout.strip() if p.returncode == 0 else ""


def push_if_needed(cfg):
    if not cfg.remote:
        return "no-remote"
    head = head_sha(cfg)
    if not head:
        return "no-commits"
    pushed_f = os.path.join(cfg.state, "last_pushed")
    try:
        last = open(pushed_f).read().strip()
    except OSError:
        last = ""
    if last == head:
        return "up-to-date"
    try:
        git(cfg, "push", "-q", "backup", f"HEAD:refs/heads/{cfg.branch}", timeout=cfg.push_timeout)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        log(f"push failed (will retry): {e}")
        return "failed"
    write_atomic(pushed_f, head)
    write_atomic(os.path.join(cfg.state, "last_push_time"), str(int(time.time())))
    log(f"pushed {head[:8]} to backup")
    return "pushed"


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def cycle(cfg):
    write_atomic(os.path.join(cfg.state, "committer.heartbeat"), str(int(time.time())))
    result = commit_if_dirty(cfg)
    pushed = push_if_needed(cfg)
    try:
        import staleness
        staleness.run(staleness.Cfg(), write_flag=True)
    except Exception as e:  # alarm must never kill the committer
        log(f"staleness check error: {e}")
    return result, pushed


def main():
    cfg = Cfg()
    os.makedirs(cfg.state, exist_ok=True)
    os.umask(int(os.environ.get("VAULT_UMASK", "000"), 8))
    while True:
        try:
            ensure_repo(cfg)
            break
        except Exception as e:
            log(f"repo setup failed: {e}; retrying in 30s")
            time.sleep(30)
    log(f"started: repo={cfg.repo} interval={cfg.interval}s debounce={cfg.debounce}s "
        f"remote={'set' if cfg.remote else 'none'}")
    while True:
        try:
            cycle(cfg)
        except Exception as e:
            log(f"cycle error: {type(e).__name__}: {e}")
        time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
