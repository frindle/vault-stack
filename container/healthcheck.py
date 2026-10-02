#!/usr/bin/env python3
"""Container HEALTHCHECK: shim answers, Basic Memory port listens, committer heartbeat fresh."""
import os
import socket
import sys
import time
import json
import urllib.request

# never use a proxy for loopback checks
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

port = os.environ.get("SHIM_PORT", "27123")
errs = []
try:
    OPENER.open(f"http://127.0.0.1:{port}/healthz", timeout=3).read()
except Exception as e:
    errs.append(f"shim: {e}")
try:
    # real round trip: an MCP initialize against BM (not just "port open")
    url = os.environ.get("BM_MCP_URL") or f"http://127.0.0.1:{os.environ.get('BM_PORT', '8000')}/mcp"
    req = urllib.request.Request(url, method="POST", data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "healthcheck", "version": "1"}}}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    body = OPENER.open(req, timeout=5).read().decode("utf-8", "replace")
    if '"result"' not in body:
        errs.append(f"basic-memory: bad initialize reply: {body[:120]}")
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
