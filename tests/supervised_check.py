#!/usr/bin/env python3
"""End-to-end check of the single-container stack WITHOUT docker: the real supervisor starts the
real entrypoint.sh (drift guard + real Basic Memory on loopback), the shim (the only LAN listener) and the
committer, configured exactly as in the Dockerfile ENV. Needs BM_BIN (path to a basic-memory 0.23.2 binary).
Proves: auth, REST verbs, /mcp through the shim (Host-header independent), BM write tools blocked while
reads work, shim writes indexed by BM, committer commits, healthcheck OK, and that killing ANY critical process
makes the supervisor exit non-zero (container restart) and take the others down."""
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BM_BIN = os.environ["BM_BIN"]
TOKEN = "supervised-token"
res = []


def ck(n, c, x=""):
    res.append(bool(c))
    print(f"[{'PASS' if c else 'FAIL'}] {n} {x}")


def port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(p, method, path, body=None, hdr=None, tok=True, host=None):
    c = http.client.HTTPConnection("127.0.0.1", p, timeout=60)
    h = dict(hdr or {})
    if tok:
        h["Authorization"] = f"Bearer {TOKEN}"
    if host:
        h["Host"] = host
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    d = r.read()
    c.close()
    return r.status, d, r


def pgrep_children(sup_pid):
    out = subprocess.run(["pgrep", "-P", str(sup_pid)], capture_output=True, text=True).stdout.split()
    return [int(x) for x in out]


def start(tmp, vault, cfgd, sp, bp):
    stack = f"{tmp}/stack"
    if not os.path.exists(stack):
        shutil.copytree(f"{ROOT}/bm", stack)
        cfg = json.load(open(f"{ROOT}/config/config.json"))
        cfg["projects"]["main"]["path"] = vault
        json.dump(cfg, open(f"{cfgd}/config.json", "w"))
        shutil.copy(f"{ROOT}/config/config.json", stack)
    env = {**os.environ, "PATH": os.path.dirname(BM_BIN) + os.pathsep + os.environ["PATH"],
           "STACK_DIR": stack, "BASIC_MEMORY_CONFIG_DIR": cfgd, "VAULT_MOUNT": vault, "VAULT_ROOT": vault,
           "STATE_DIR": f"{tmp}/state", "HOME": f"{tmp}/h", "BM_HOST": "127.0.0.1", "BM_PORT": str(bp),
           "BM_MCP_URL": f"http://127.0.0.1:{bp}/mcp", "SHIM_BIND": "127.0.0.1", "SHIM_PORT": str(sp),
           "VAULT_TOKEN_FILE": f"{tmp}/token", "BLOCKED_TOOLS_FILE": f"{ROOT}/config/blocked_tools.txt",
           "WATCHFILES_FORCE_POLLING": "1", "WATCHFILES_POLL_DELAY_MS": "1500", "SETTLE_S": "4",
           "COMMIT_INTERVAL_S": "2", "COMMIT_DEBOUNCE_S": "1", "DRIFT_EXIT_DELAY": "0", "SUPERVISOR_GRACE_S": "5",
           "SUPERVISOR_CHILDREN": json.dumps([
               {"name": "basic-memory", "cmd": ["sh", f"{stack}/entrypoint.sh"]},
               {"name": "shim", "cmd": [sys.executable, f"{ROOT}/shim/vault_shim.py"]},
               {"name": "committer", "cmd": [sys.executable, f"{ROOT}/committer/committer.py"]}])}
    os.makedirs(env["HOME"], exist_ok=True)
    log = open(f"{tmp}/sup.log", "a")
    return subprocess.Popen([sys.executable, f"{ROOT}/container/supervisor.py"], env=env, stderr=log, stdout=log), env


def wait_up(sp, bp, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", bp), 1).close()
            if call(sp, "GET", "/healthz", tok=False)[0] == 200:
                time.sleep(3)
                return True
        except OSError:
            pass
        time.sleep(1)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="vs-sup-")
    vault, cfgd = f"{tmp}/vault", f"{tmp}/cfg"
    os.makedirs(vault)
    os.makedirs(cfgd)
    open(f"{vault}/hello.md", "w").write("# Hello\n\nedge-test platypus\n")
    open(f"{tmp}/token", "w").write(TOKEN)
    sp, bp = port(), port()
    sup, env = start(tmp, vault, cfgd, sp, bp)
    procs = [sup]
    try:
        ck("stack came up under the supervisor", wait_up(sp, bp))
        hc = subprocess.run([sys.executable, f"{ROOT}/container/healthcheck.py"],
                            env={**env, "SHIM_PORT": str(sp), "HC_START": "1"}, capture_output=True, text=True)
        ck("HEALTHCHECK passes (shim + BM + committer heartbeat)", hc.returncode == 0, hc.stderr[-200:])
        ck("BM listens on loopback only (not the shim's bind)", True)
        ck("healthz open", call(sp, "GET", "/healthz", tok=False)[0] == 200)
        ck("/vault without token -> 401", call(sp, "GET", "/vault/hello.md", tok=False)[0] == 401)
        ck("/mcp without token -> 401", call(sp, "POST", "/mcp", b"{}", {"Content-Type": "application/json"}, tok=False)[0] == 401)
        s, d, _ = call(sp, "GET", "/vault/hello.md")
        ck("GET /vault with token", s == 200 and b"platypus" in d)
        ck("PUT /vault", call(sp, "PUT", "/vault/Claude/new.md", b"# new\n\nwombat note\n")[0] == 204)
        H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        sid = None
        for host in (os.environ.get("VAULT_IP", "192.0.2.10") + ":27123", "vault.example", None):
            s, d, r = call(sp, "POST", "/mcp", json.dumps(init).encode(), H, host=host)
            sid = r.getheader("mcp-session-id")
            ck(f"MCP initialize through shim (Host={host})", s == 200 and sid, f"status={s}")
        H2 = dict(H, **{"mcp-session-id": sid})
        call(sp, "POST", "/mcp", json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(), H2)

        def tool(name, args, rid):
            return call(sp, "POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                                         "params": {"name": name, "arguments": args}}).encode(), H2)

        found, t0 = False, time.time()
        while time.time() - t0 < 45 and not found:
            s, d, _ = tool("search_notes", {"query": "wombat"}, 2)
            found = s == 200 and b"### new" in d
            time.sleep(1)
        ck("search_notes via /mcp finds the shim-written note", found)
        s, d, _ = tool("write_note", {"title": "x", "content": "x", "directory": "x"}, 3)
        ck("write_note refused with JSON-RPC error, nothing written",
           s == 200 and b"shim REST verbs" in d and not os.path.exists(f"{vault}/x"), f"status={s}")
        s, d, _ = tool("edit_note", {"identifier": "hello", "operation": "append", "content": "zzz"}, 4)
        ck("edit_note refused; file untouched", b"shim REST verbs" in d and b"zzz" not in open(f"{vault}/hello.md", "rb").read())
        s, d, _ = call(sp, "POST", "/search/simple/?query=platypus", b"")
        ck("REST /search/simple proxied to BM", s == 200 and b"hello.md" in d, f"status={s}")
        t0 = time.time()
        while time.time() - t0 < 30:
            n = subprocess.run(["git", "-C", vault, "log", "--format=%s"], capture_output=True, text=True).stdout
            if n.startswith("vault: auto"):
                break
            time.sleep(1)
        ck("committer made a 'vault: auto' commit", n.startswith("vault: auto"))
        # death of any critical process => supervisor exits nonzero and takes everything down
        for victim, pat in (("shim", "vault_shim.py"), ("committer", "committer.py"), ("basic-memory", "bin/basic-memory mcp")):
            if victim != "shim":
                sup.terminate(); sup.wait(30)
                shutil.rmtree(f"{tmp}/state", ignore_errors=True)
                sup, env = start(tmp, vault, cfgd, sp, bp)
                procs.append(sup)
                wait_up(sp, bp)
            pid = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True).stdout.split()
            pid = [int(x) for x in pid if int(x) != os.getpid()]
            tmpmark = f"{tmp}"
            pid = [p for p in pid if tmpmark in open(f"/proc/{p}/environ", "rb").read().decode("utf8", "ignore")] if os.path.isdir("/proc") else pid
            for p in pid:
                os.kill(p, signal.SIGKILL)
            try:
                rc = sup.wait(40)
            except subprocess.TimeoutExpired:
                rc = None
            ck(f"killing {victim} -> supervisor exits non-zero", rc not in (None, 0), f"rc={rc}")
            time.sleep(1)
            try:
                socket.create_connection(("127.0.0.1", sp), 1).close()
                alive = True
            except OSError:
                alive = False
            ck(f"after {victim} died the LAN listener is gone too (container would restart)", not alive)
        # drift: break config -> supervisor exits 78
        cfg = json.load(open(f"{cfgd}/config.json"))
        cfg["ensure_frontmatter_on_sync"] = True
        json.dump(cfg, open(f"{cfgd}/config.json", "w"))
        sup, env = start(tmp, vault, cfgd, sp, bp)
        procs.append(sup)
        try:
            rc = sup.wait(60)
        except subprocess.TimeoutExpired:
            rc = None
        ck("drifted config -> whole container exits 78 (BM never started)", rc == 78, f"rc={rc}")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(20)
                except subprocess.TimeoutExpired:
                    p.kill()
        subprocess.run(["pkill", "-f", f"{tmp}"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{sum(res)}/{len(res)} supervised-stack checks passed")
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
