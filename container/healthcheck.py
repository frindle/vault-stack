#!/usr/bin/env python3
"""Container HEALTHCHECK: shim answers, Basic Memory port listens, committer heartbeat fresh."""
import os
import socket
import sys
import time
import urllib.request

port = os.environ.get("SHIM_PORT", "27123")
errs = []
try:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3).read()
except Exception as e:
    errs.append(f"shim: {e}")
try:
    socket.create_connection(("127.0.0.1", int(os.environ.get("BM_PORT", "8000"))), 3).close()
except Exception as e:
    errs.append(f"basic-memory: {e}")
try:
    hb = float(open(os.path.join(os.environ.get("STATE_DIR", "/config/state"), "committer.heartbeat")).read())
    if time.time() - hb > float(os.environ.get("HEARTBEAT_MAX_S", "900")):
        errs.append("committer heartbeat stale")
except Exception as e:
    if time.time() - float(os.environ.get("HC_START", "0")) > 300:
        errs.append(f"committer heartbeat: {e}")
if errs:
    sys.stderr.write("UNHEALTHY: " + "; ".join(errs) + "\n")
    sys.exit(1)
