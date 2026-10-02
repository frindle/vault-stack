#!/usr/bin/env python3
"""Integration test against a REAL Basic Memory instance (needs basic-memory installed).

  BM_BIN=/path/to/basic-memory python3 tests/integration_bm.py

Builds a SYNTHETIC vault, starts BM (streamable-http, polling watcher) and the shim,
then proves: BM does not rewrite pre-existing files, oversize files are not indexed,
shim-written files (PUT / append / PATCH / move / delete) become searchable within
seconds, and a 20-way concurrent append through the shim loses 0 lines AND all lines
are searchable afterwards.
"""
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BM_BIN = os.environ.get("BM_BIN", "basic-memory")
TOKEN = "integration-token"
results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def sha_tree(root):
    out = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    h = {"Authorization": f"Bearer {TOKEN}", **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    d = r.read()
    c.close()
    return r.status, d


def search(port, q):
    s, d = req(port, "POST", "/search/simple/?query=" + q.replace(" ", "%20"), b"")
    return s, (json.loads(d) if s == 200 else d)


def wait_found(port, q, want_path=None, timeout=45, present=True):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s, res = search(port, q)
        if s == 200:
            hit = [r["filename"] for r in res]
            if present and (hit if want_path is None else want_path in hit):
                return time.time() - t0, hit
            if not present and not hit:
                return time.time() - t0, hit
        time.sleep(0.5)
    return None, None


def main():
    tmp = tempfile.mkdtemp(prefix="vs-int-")
    vault, cfgdir, state = f"{tmp}/vault", f"{tmp}/cfg", f"{tmp}/state"
    os.makedirs(f"{vault}/Claude/Projects")
    os.makedirs(f"{vault}/Claude/Handoff")
    os.makedirs(cfgdir)
    for i in range(40):  # no frontmatter: BM defaults would rewrite all of these
        open(f"{vault}/Claude/Projects/note{i}.md", "w").write(f"# Note {i}\n\nplain text about topic{i}\n")
    open(f"{vault}/Claude/Handoff/fm.md", "w").write("---\ntitle: Fm\ntags: [x]\n---\nbody zebra\n")
    open(f"{vault}/Claude/Big-Log.md", "w").write("bigfileneedle " + "a" * (2_500_000))
    open(f"{vault}/Claude/Log-2026-10.md", "w").write("# Log\n")
    before = sha_tree(vault)

    cfg = json.load(open(os.path.join(ROOT, "config", "config.json")))
    cfg["projects"]["main"]["path"] = vault
    json.dump(cfg, open(f"{cfgdir}/config.json", "w"))
    envbase = {**os.environ, "BASIC_MEMORY_CONFIG_DIR": cfgdir, "HOME": f"{tmp}/home", "VAULT_MOUNT": vault,
               "WATCHFILES_FORCE_POLLING": "1", "WATCHFILES_POLL_DELAY_MS": "1500"}
    os.makedirs(envbase["HOME"])
    g = subprocess.run([sys.executable, f"{ROOT}/bm/drift_guard.py", f"{cfgdir}/config.json"], env=envbase)
    check("drift guard accepts the safe config", g.returncode == 0)
    subprocess.run([sys.executable, f"{ROOT}/bm/make_bmignore.py"], env=envbase, check=True)

    bmport, shimport = free_port(), free_port()
    bm = subprocess.Popen([BM_BIN, "mcp", "--transport", "streamable-http", "--host", "127.0.0.1",
                           "--port", str(bmport), "--path", "/mcp"], env=envbase,
                          stdout=open(f"{tmp}/bm.log", "w"), stderr=subprocess.STDOUT)
    open(f"{tmp}/token", "w").write(TOKEN)
    shim = subprocess.Popen([sys.executable, f"{ROOT}/shim/vault_shim.py"], stderr=open(f"{tmp}/shim.log", "w"),
                            env={**envbase, "VAULT_ROOT": vault, "VAULT_TOKEN_FILE": f"{tmp}/token",
                                 "SHIM_PORT": str(shimport), "STATE_DIR": state,
                                 "BM_MCP_URL": f"http://127.0.0.1:{bmport}/mcp"})
    try:
        t0 = time.time()
        while time.time() - t0 < 90:
            try:
                if req(shimport, "GET", "/healthz")[0] == 200 and search(shimport, "zebra")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        lat, hit = wait_found(shimport, "zebra", "Claude/Handoff/fm.md")
        check("BM indexed pre-existing vault", lat is not None, f"(initial index {time.time() - t0:.1f}s)")
        check("BM left every pre-existing file byte-identical", sha_tree(vault) == before)
        s, res = search(shimport, "bigfileneedle")
        check("oversize (>2MB) file is NOT indexed (.bmignore)", s == 200 and res == [])

        # shim writes -> BM indexes
        assert req(shimport, "PUT", "/vault/Claude/Projects/shimmed.md", b"# Shimmed\n\nquokka marker\n")[0] == 204
        lat, hit = wait_found(shimport, "quokka", "Claude/Projects/shimmed.md")
        check("PUT via shim searchable in BM", lat is not None, f"({lat and round(lat, 1)}s)")
        assert req(shimport, "POST", "/vault/Claude/Log-2026-10.md", b"- entry with wombat marker\n")[0] == 204
        lat, hit = wait_found(shimport, "wombat", "Claude/Log-2026-10.md")
        check("append via shim searchable in BM", lat is not None, f"({lat and round(lat, 1)}s)")
        h = {"Operation": "append", "Target-Type": "heading", "Target": "Shimmed"}
        assert req(shimport, "PATCH", "/vault/Claude/Projects/shimmed.md", b"patched with axolotl", h)[0] == 200
        lat, hit = wait_found(shimport, "axolotl", "Claude/Projects/shimmed.md")
        check("PATCH via shim searchable in BM", lat is not None, f"({lat and round(lat, 1)}s)")
        assert req(shimport, "POST", "/_move", json.dumps({"from": "Claude/Projects/shimmed.md",
                                                           "to": "Claude/Projects/renamed.md"}).encode())[0] == 200
        lat, hit = wait_found(shimport, "quokka", "Claude/Projects/renamed.md")
        check("move via shim: new path searchable", lat is not None, f"({lat and round(lat, 1)}s)")
        lat, hit = wait_found(shimport, "quokka", present=False) if False else (None, None)
        s, res = search(shimport, "quokka")
        check("move via shim: old path gone from index", all(r["filename"] != "Claude/Projects/shimmed.md" for r in res))
        assert req(shimport, "DELETE", "/vault/Claude/Projects/renamed.md")[0] == 204
        lat, _ = wait_found(shimport, "quokka", present=False)
        check("delete via shim removed from BM index", lat is not None, f"({lat and round(lat, 1)}s)")

        # 20-way concurrent append through the shim, then BM finds them
        W, N = 20, 10
        errs = []

        def worker(w):
            for i in range(N):
                try:
                    s, _ = req(shimport, "POST", "/vault/Claude/Log-2026-10.md", f"- cw{w:02d}x{i:02d} giraffe\n".encode())
                    if s != 204:
                        errs.append(s)
                except Exception as e:
                    errs.append(repr(e))

        ts = [threading.Thread(target=worker, args=(w,)) for w in range(W)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        lines = [l for l in open(f"{vault}/Claude/Log-2026-10.md").read().splitlines() if "giraffe" in l]
        check("20-way concurrent append through shim loses 0 lines", not errs and len(lines) == W * N and
              len(set(lines)) == W * N, f"({len(lines)}/{W * N} lines, errors={errs[:3]})")
        lat, hit = wait_found(shimport, "cw19x09", "Claude/Log-2026-10.md")
        check("last concurrently-appended line searchable in BM", lat is not None, f"({lat and round(lat, 1)}s)")
        if lat is None:
            for q in ("cw00x00", "cw19x09", "cw10x05"):
                print("diag", q, search(shimport, q)[1])
            print("--- bm.log (warnings/errors) ---")
            print("\n".join(l for l in open(f"{tmp}/bm.log").read().splitlines() if re.search(r"WARN|ERROR|rror|locked|fail", l))[-3000:])

        # size guard against the real file path
        big = "- " + "z" * 1_000_000 + "\n"
        statuses = [req(shimport, "POST", "/vault/Claude/Log-2026-10.md", big.encode())[0] for _ in range(3)]
        check("size guard refuses the append that crosses 2MB (204,204,413)", statuses == [204, 204, 413], str(statuses))
        check("file stayed under cap", os.path.getsize(f"{vault}/Claude/Log-2026-10.md") <= 2 * 1024 * 1024)
        s, _ = search(shimport, "giraffe")
        check("BM still answers after near-cap writes", s == 200)
        check("BM log has no traceback", "Traceback" not in open(f"{tmp}/bm.log").read())
    finally:
        for p in (shim, bm):
            p.send_signal(signal.SIGTERM)
        for p in (shim, bm):
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} integration checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
