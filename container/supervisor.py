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


def main():
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
