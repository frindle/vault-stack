#!/usr/bin/env python3
"""Tiny supervisor for the single vault container (stdlib only, PID 1).

Runs Basic Memory (127.0.0.1), the write shim (LAN listener :27123) and the committer.
Policy: ALL children are critical. If any one exits (any code, even 0) the rest are
terminated and the supervisor exits NON-ZERO, so Docker's restart policy restarts the whole
container. No in-place restarts: a half-alive stack (shim up, BM dead; BM up, committer
dead) would silently stop indexing or stop backing up.

SIGTERM/SIGINT (docker stop) -> terminate children, exit 0.
Override the child list for tests with SUPERVISOR_CHILDREN='[{"name":..,"cmd":[..]},...]'.
"""
import json
import os
import signal
import subprocess
import sys
import time

DEFAULT_CHILDREN = [
    {"name": "basic-memory", "cmd": ["sh", "/opt/vault-stack/entrypoint.sh"]},
    {"name": "shim", "cmd": ["python3", "/app/shim/vault_shim.py"]},
    {"name": "committer", "cmd": ["python3", "/app/committer/committer.py"]},
]
GRACE_S = float(os.environ.get("SUPERVISOR_GRACE_S", "10"))


def log(msg):
    sys.stderr.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} supervisor: {msg}\n")
    sys.stderr.flush()


def stop_all(procs, sig=signal.SIGTERM):
    for name, p in procs.items():
        if p.poll() is None:
            try:
                os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
    deadline = time.time() + GRACE_S
    for name, p in procs.items():
        try:
            p.wait(max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            log(f"{name} ignored SIGTERM, killing")
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            p.wait()


TOKEN_HELP = ("secrets/vault_token must be a file with the token (openssl rand -hex 32 > secrets/vault_token); "
              "a directory means it was missing when compose first ran: rm -rf it and recreate")


def ids():
    return os.getuid(), os.getgid()


def token_problem(path):
    """None if the token file is usable, else (reason, exact fix)."""
    uid, gid = ids()
    if os.path.isdir(path):
        return (f"{path} is a DIRECTORY", TOKEN_HELP)
    if not os.path.exists(path):
        return (f"{path} is missing", TOKEN_HELP)
    if not os.access(path, os.R_OK):
        return (f"{path} exists but is NOT READABLE by uid {uid}:{gid} (this container's user)",
                f"on the host, in the stack dir: chown {uid}:{gid} secrets/vault_token && chmod 400 secrets/vault_token")
    try:
        if not open(path).read().strip():
            return (f"{path} is empty", TOKEN_HELP)
    except OSError as e:
        return (f"{path} cannot be read: {e}", f"chown {uid}:{gid} secrets/vault_token && chmod 400 secrets/vault_token")
    return None


def dir_problem(path, label, env_hint):
    """None if the mounted dir is writable by this uid, else (reason, exact fix)."""
    uid, gid = ids()
    fix = f"on the host: mkdir -p <{env_hint}> && chown -R {uid}:{gid} <{env_hint}>   (the folder mounted at {path})"
    if not os.path.isdir(path):
        return (f"{label} {path} is missing", fix)
    if not os.access(path, os.W_OK | os.X_OK):
        return (f"{label} {path} is NOT WRITABLE by uid {uid}:{gid} (this container's user)", fix)
    return None


def preflight(env=None):
    """List of (reason, fix, fatal) problems; fatal ones would crash-loop the stack."""
    env = os.environ if env is None else env
    out = []
    tf = env.get("VAULT_TOKEN_FILE", "")
    if tf:
        p = token_problem(tf)
        if p:
            out.append((p[0], p[1], True))
    for var, label, hint in (("BASIC_MEMORY_CONFIG_DIR", "state dir", "APPDATA_DIR"),
                             ("VAULT_ROOT", "vault dir", "VAULT_DIR")):
        if env.get(var):
            p = dir_problem(env[var], label, hint)
            if p:
                out.append((p[0], p[1], True))
    remote = env.get("COMMITTER_REMOTE", "").strip()
    if remote.startswith("/"):
        p = dir_problem(os.path.dirname(remote), "backup dir", "BACKUP_DIR")
        if p:
            out.append((p[0], p[1], False))  # not fatal: the committer degrades to local commits
    return out


def main():
    problems = preflight()
    for why, fix, fatal in problems:
        log(f"{'REFUSING TO START' if fatal else 'WARNING (continuing)'}: {why}. FIX: {fix}")
    if any(f for _, _, f in problems):
        time.sleep(float(os.environ.get("TOKEN_EXIT_DELAY", "10")))  # avoid a hot restart loop
        return 78
    children = json.loads(os.environ["SUPERVISOR_CHILDREN"]) if os.environ.get("SUPERVISOR_CHILDREN") \
        else DEFAULT_CHILDREN
    stopping = []
    procs = {}

    def on_signal(signum, frame):
        stopping.append(signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    for c in children:
        procs[c["name"]] = subprocess.Popen(c["cmd"], start_new_session=True)  # own process group
        log(f"started {c['name']} pid={procs[c['name']].pid}")
    rc = 0
    while True:
        if stopping:
            log("signal received, shutting down")
            break
        dead = [(n, p.returncode) for n, p in procs.items() if p.poll() is not None]
        if dead:
            n, code = dead[0]
            rc = code if code not in (0, None) and code > 0 else 1
            log(f"CRITICAL child {n} exited with {code}; stopping everything, exiting {rc} so Docker restarts the container")
            break
        time.sleep(0.3)
    stop_all(procs)
    return rc


if __name__ == "__main__":
    sys.exit(main())
