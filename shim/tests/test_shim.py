import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("SHIM_SRC") or os.path.join(HERE, ".."))
import vault_shim as vs  # noqa: E402

TOKEN = "s3cret-token-for-tests"


def rd(p):
    with open(p) as fh:
        return fh.read()


class Base(unittest.TestCase):
    cap = 2 * 1024 * 1024

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "vault")
        os.makedirs(self.root)
        tf = os.path.join(self.tmp.name, "token")
        with open(tf, "w") as f:
            f.write(TOKEN + "\n")
        env = {"VAULT_ROOT": self.root, "VAULT_TOKEN_FILE": tf, "SHIM_BIND": "127.0.0.1", "SHIM_PORT": "0",
               "MAX_NOTE_BYTES": str(self.cap), "STATE_DIR": os.path.join(self.tmp.name, "state")}
        self.cfg = vs.Config(env)
        self.bm = None
        self.start()

    def start(self):
        self.srv = vs.make_server(self.cfg, self.bm)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        self.log = io.StringIO()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, token=TOKEN):
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        hd = dict(r.getheaders())
        c.close()
        return r.status, data, hd

    def f(self, rel):
        return os.path.join(self.root, rel)


class TestVerbs(Base):
    def test_auth(self):
        self.assertEqual(self.req("GET", "/vault/a.md", token=None)[0], 401)
        self.assertEqual(self.req("GET", "/vault/a.md", token="nope")[0], 401)
        self.assertEqual(self.req("GET", "/vault/a.md", token=TOKEN[:-1])[0], 401)
        self.assertEqual(self.req("GET", "/_auth")[0], 204)
        self.assertEqual(self.req("GET", "/_auth", token=None)[0], 401)
        self.assertEqual(self.req("GET", "/healthz", token=None)[0], 200)
        # raw (non-Bearer) header is rejected
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", "/_auth", headers={"Authorization": TOKEN})
        self.assertEqual(c.getresponse().status, 401)

    def test_token_not_in_logs(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.req("GET", "/vault/a.md")
            self.req("GET", "/vault/a.md", token="wrong-token-xyz")
        self.assertNotIn(TOKEN, buf.getvalue())
        self.assertNotIn("wrong-token-xyz", buf.getvalue())
        self.assertIn("GET /vault/a.md 404", buf.getvalue())

    def test_token_hot_reload(self):
        with open(self.cfg.token_file, "w") as f:
            f.write("Bearer rotated-token-longer\n")
        self.assertEqual(self.req("GET", "/_auth")[0], 401)
        self.assertEqual(self.req("GET", "/_auth", token="rotated-token-longer")[0], 204)

    def test_put_get_head_etag(self):
        s, _, h = self.req("PUT", "/vault/Claude/n.md", b"hello\n")
        self.assertEqual(s, 204)
        s, d, h = self.req("GET", "/vault/Claude/n.md")
        self.assertEqual((s, d), (200, b"hello\n"))
        s, d, h2 = self.req("HEAD", "/vault/Claude/n.md")
        self.assertEqual(h["ETag"], h2["ETag"])
        self.assertEqual(rd(self.f("Claude/n.md")), "hello\n")
        # no temp litter
        self.assertEqual(os.listdir(self.f("Claude")), ["n.md"])

    def test_post_append_and_404(self):
        self.assertEqual(self.req("POST", "/vault/x.md", b"a")[0], 404)
        self.req("PUT", "/vault/x.md", b"one\n")
        self.assertEqual(self.req("POST", "/vault/x.md", b"two\n")[0], 204)
        self.req("PUT", "/vault/y.md", b"no-newline")
        self.req("POST", "/vault/y.md", b"next\n")
        self.assertEqual(rd(self.f("x.md")), "one\ntwo\n")
        self.assertEqual(rd(self.f("y.md")), "no-newline\nnext\n")
        self.assertEqual(self.req("POST", "/vault/new.md", b"made\n", {"X-Create-If-Missing": "true"})[0], 204)
        self.assertEqual(rd(self.f("new.md")), "made\n")

    def test_if_match(self):
        self.req("PUT", "/vault/m.md", b"v1\n")
        _, _, h = self.req("HEAD", "/vault/m.md")
        self.assertEqual(self.req("PUT", "/vault/m.md", b"v2\n", {"If-Match": h["ETag"]})[0], 204)
        self.assertEqual(self.req("PUT", "/vault/m.md", b"v3\n", {"If-Match": h["ETag"]})[0], 412)
        self.assertEqual(self.req("PUT", "/vault/m.md", b"v4\n", {"If-None-Match": "*"})[0], 412)
        self.assertEqual(rd(self.f("m.md")), "v2\n")

    def test_patch_heading(self):
        self.req("PUT", "/vault/p.md", b"# T\n\n## A\nold a\n\n## B\nbee\n")
        h = {"Operation": "append", "Target-Type": "heading", "Target": "A"}
        self.assertEqual(self.req("PATCH", "/vault/p.md", b"added", h)[0], 200)
        self.assertEqual(rd(self.f("p.md")), "# T\n\n## A\nold a\nadded\n\n## B\nbee\n")
        h["Operation"] = "replace"
        self.req("PATCH", "/vault/p.md", b"fresh", h)
        self.assertEqual(rd(self.f("p.md")), "# T\n\n## A\n\nfresh\n\n## B\nbee\n")
        h = {"Operation": "prepend", "Target-Type": "heading", "Target": "T::B"}
        self.assertEqual(self.req("PATCH", "/vault/p.md", b"first", h)[0], 200)
        self.assertIn("## B\nfirst\nbee\n", rd(self.f("p.md")))
        h["Target"] = "Nope"
        self.assertEqual(self.req("PATCH", "/vault/p.md", b"x", h)[0], 404)

    def test_patch_frontmatter(self):
        self.req("PUT", "/vault/f.md", b"---\ntitle: X\n---\nbody\n")
        h = {"Operation": "replace", "Target-Type": "frontmatter", "Target": "status"}
        self.req("PATCH", "/vault/f.md", b'"done"', h)
        self.assertEqual(rd(self.f("f.md")), "---\ntitle: X\nstatus: done\n---\nbody\n")
        h["Target"] = "title"
        self.req("PATCH", "/vault/f.md", b"Y", h)
        self.assertIn("title: Y", rd(self.f("f.md")))

    def test_delete_move_copy_list(self):
        self.req("PUT", "/vault/d/a.md", b"A\n")
        self.req("PUT", "/vault/d/.hidden.md", b"h\n") if False else None
        s, d, _ = self.req("GET", "/vault/d/")
        self.assertEqual(json.loads(d), {"files": ["a.md"]})
        s, d, _ = self.req("GET", "/vault/")
        self.assertEqual(json.loads(d), {"files": ["d/"]})
        self.assertEqual(self.req("POST", "/_copy", json.dumps({"from": "d/a.md", "to": "d/b.md"}).encode())[0], 200)
        self.assertEqual(self.req("POST", "/_copy", json.dumps({"from": "d/a.md", "to": "d/b.md"}).encode())[0], 409)
        self.assertEqual(self.req("POST", "/_move", json.dumps({"from": "d/b.md", "to": "e/c.md"}).encode())[0], 200)
        self.assertFalse(os.path.exists(self.f("d/b.md")))
        self.assertEqual(rd(self.f("e/c.md")), "A\n")
        self.assertEqual(self.req("DELETE", "/vault/e/c.md")[0], 204)
        self.assertEqual(self.req("DELETE", "/vault/e/c.md")[0], 404)

    def test_path_safety(self):
        for bad in ("/vault/../etc/passwd", "/vault/%2e%2e/x.md", "/vault/.git/config", "/vault/a/.obsidian/x.md"):
            self.assertEqual(self.req("PUT", bad, b"x")[0], 400, bad)
        os.symlink("/etc", self.f("link"))
        self.assertEqual(self.req("GET", "/vault/link/passwd")[0], 400)

    def test_search_jsonlogic_like_vault_drain(self):
        self.req("PUT", "/vault/Claude/a.md", b"alpha\n")
        self.req("PUT", "/vault/Projects/b.md", b"beta\n")
        self.req("PUT", "/vault/Other/c.md", b"gamma\n")
        q = {"if": [{"or": [{"glob": ["Claude/**", {"var": "path"}]}, {"glob": ["Projects/**", {"var": "path"}]}]},
                    {"var": "stat"}, False]}
        s, d, _ = self.req("POST", "/search/", json.dumps(q).encode(),
                           {"Content-Type": "application/vnd.olrapi.jsonlogic+json"})
        res = json.loads(d)
        self.assertEqual(sorted(r["filename"] for r in res), ["Claude/a.md", "Projects/b.md"])
        self.assertEqual(set(res[0]["result"]), {"ctime", "mtime", "size"})
        q2 = {"regexp": ["gam+a", {"var": "content"}]}
        res = json.loads(self.req("POST", "/search/", json.dumps(q2).encode())[1])
        self.assertEqual([r["filename"] for r in res], ["Other/c.md"])

    def test_status_reads_stale_flag(self):
        os.makedirs(self.cfg.state_dir)
        with open(os.path.join(self.cfg.state_dir, "STALE"), "w") as fh:
            fh.write('{"reasons":["x"]}')
        self.assertEqual(json.loads(self.req("GET", "/_status")[1])["stale"], {"reasons": ["x"]})

    def test_chunked_body(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.putrequest("PUT", "/vault/ch.md")
        c.putheader("Authorization", f"Bearer {TOKEN}")
        c.putheader("Transfer-Encoding", "chunked")
        c.endheaders()
        c.send(b"3\r\nabc\r\n3\r\ndef\r\n0\r\n\r\n")
        self.assertEqual(c.getresponse().status, 204)
        self.assertEqual(rd(self.f("ch.md")), "abcdef")


class TestSettleTouch(Base):
    def start(self):
        self.cfg.settle_s = 0.4
        super().start()

    def test_mtime_bumped_after_quiet_period_content_unchanged(self):
        self.req("PUT", "/vault/s.md", b"one\n")
        os.utime(self.f("s.md"), (1000, 1000))  # pretend BM saw an old mtime
        self.req("POST", "/vault/s.md", b"two\n")
        os.utime(self.f("s.md"), (1000, 1000))
        for _ in range(40):
            time.sleep(0.1)
            if os.stat(self.f("s.md")).st_mtime > 2000:
                break
        self.assertGreater(os.stat(self.f("s.md")).st_mtime, 2000, "settle touch did not happen")
        self.assertEqual(rd(self.f("s.md")), "one\ntwo\n")


class TestConcurrency(Base):
    def test_20_way_concurrent_append_loses_zero_lines(self):
        self.req("PUT", "/vault/Claude/log.md", b"# log\n")
        W, N = 20, 15
        errs = []

        def worker(w):
            for i in range(N):
                try:
                    s, d, _ = self.req("POST", "/vault/Claude/log.md", f"- w{w:02d} i{i:02d}\n".encode())
                    if s != 204:
                        errs.append((s, d))
                except Exception as e:  # a client-side failure is a failure, not a silent loss
                    errs.append(repr(e))

        ts = [threading.Thread(target=worker, args=(w,)) for w in range(W)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        lines = rd(self.f("Claude/log.md")).splitlines()
        self.assertEqual(lines[0], "# log")
        body = lines[1:]
        self.assertEqual(len(body), W * N, f"lost {W * N - len(body)} lines")
        self.assertEqual(len(set(body)), W * N)
        self.assertEqual([n for n in os.listdir(self.f("Claude")) if n.endswith(".tmp")], [])


class TestSizeGuard(Base):
    cap = 4096

    def test_append_over_cap_refused_with_roll_hint(self):
        self.req("PUT", "/vault/Claude/Activity-Log-2026-12.md", b"x" * 4000)
        s, d, _ = self.req("POST", "/vault/Claude/Activity-Log-2026-12.md", b"y" * 200)
        self.assertEqual(s, 413)
        msg = json.loads(d)["message"]
        self.assertIn("SIZE GUARD", msg)
        self.assertIn("Activity-Log-2027-01.md", msg)
        self.assertEqual(os.path.getsize(self.f("Claude/Activity-Log-2026-12.md")), 4000)

    def test_put_over_cap_refused_but_shrink_allowed(self):
        self.assertEqual(self.req("PUT", "/vault/big.md", b"z" * 5000)[0], 413)
        self.assertFalse(os.path.exists(self.f("big.md")))
        with open(self.f("legacy.md"), "wb") as fh:  # pre-existing oversize note (arrived via SMB)
            fh.write(b"q" * 9000)
        self.assertEqual(self.req("PUT", "/vault/legacy.md", b"q" * 8000)[0], 204)
        self.assertEqual(self.req("POST", "/vault/legacy.md", b"more")[0], 413)

    def test_body_over_cap_refused(self):
        self.assertEqual(self.req("PUT", "/vault/huge.md", b"h" * (self.cap + 70000))[0], 413)

    def test_patch_over_cap_refused(self):
        self.req("PUT", "/vault/s.md", b"# H\n" + b"a" * 4000)
        h = {"Operation": "append", "Target-Type": "heading", "Target": "H"}
        self.assertEqual(self.req("PATCH", "/vault/s.md", b"b" * 500, h)[0], 413)


class FakeBM(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fails = 0
    calls = 0
    sse = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        msg = json.loads(self.rfile.read(n))
        cls = type(self)
        if msg.get("method") == "initialize":
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-03-26"}}
            return self._reply(out, sid="sess1")
        if "id" not in msg:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        cls.calls += 1
        if cls.calls <= cls.fails:
            text = "Error: entity was modified concurrently, retry"
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}],
                                                                  "isError": False}}
        else:
            payload = {"results": [{"title": "T", "file_path": "Claude/a.md", "content": "snip", "score": 1.5}]}
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                                                                  "structuredContent": {"result": payload}}}
        self._reply(out)

    def _reply(self, out, sid=None):
        if self.sse:
            body = ("event: message\ndata: " + json.dumps(out) + "\n\n").encode()
            ct = "text/event-stream"
        else:
            body, ct = json.dumps(out).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        if sid:
            self.send_header("mcp-session-id", sid)
        self.end_headers()
        self.wfile.write(body)


class TestBMProxy(Base):
    def setUp(self):
        FakeBM.fails, FakeBM.calls, FakeBM.sse = 0, 0, False
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeBM)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.bmurl = f"http://127.0.0.1:{self.fake.server_address[1]}/mcp"
        super().setUp()
        self.bm = None

    def start(self):
        self.bm = vs.BMClient(self.bmurl, timeout=5, retries=3, backoff=0.01)
        super().start()

    def tearDown(self):
        self.fake.shutdown()
        self.fake.server_close()
        super().tearDown()

    def test_search_ok(self):
        s, d, _ = self.req("POST", "/search/simple/?query=snip", b"")
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(d)[0]["filename"], "Claude/a.md")

    def test_search_ignores_proxy_env(self):
        keys = ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"]
        drop = ["NO_PROXY", "no_proxy"]
        saved = {k: os.environ.get(k) for k in keys + drop}
        try:
            for k in drop:
                os.environ.pop(k, None)
            for k in keys:
                os.environ[k] = "http://proxy.invalid.example:3128"
            s, d, _ = self.req("POST", "/search/simple/?query=snip", b"")
            self.assertEqual(s, 200, d)
            self.assertEqual(json.loads(d)[0]["filename"], "Claude/a.md")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_unreachable_error_names_real_cause(self):
        bm = vs.BMClient("http://127.0.0.1:1/mcp", timeout=1, retries=0, backoff=0.01)
        with self.assertRaises(vs.BMError) as cm:
            bm.call("search_notes", {"query": "x"})
        self.assertIn("127.0.0.1:1", str(cm.exception))

    def test_search_sse(self):
        FakeBM.sse = True
        s, d, _ = self.req("POST", "/search/simple/?query=snip", b"")
        self.assertEqual(json.loads(d)[0]["matches"][0]["context"], "snip")

    def test_concurrency_text_is_failure_then_retried(self):
        FakeBM.fails = 2
        s, d, _ = self.req("POST", "/search/simple/?query=q", b"")
        self.assertEqual(s, 200)
        self.assertEqual(FakeBM.calls, 3)

    def test_persistent_concurrency_text_is_502_not_success(self):
        FakeBM.fails = 99
        s, d, _ = self.req("POST", "/search/simple/?query=q", b"")
        self.assertEqual(s, 502)
        self.assertIn("concurrency", json.loads(d)["message"])

    def test_bm_down_is_502(self):
        self.fake.shutdown()
        self.fake.server_close()
        s, d, _ = self.req("POST", "/search/simple/?query=q", b"")
        self.assertEqual(s, 502)


class FakeMCP(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        msg = json.loads(self.rfile.read(n))
        type(self).seen.append(msg)
        body = ("event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                                                        "result": {"ok": True}}) + "\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("mcp-session-id", "s9")
        self.end_headers()
        self.wfile.write(body)


class TestMCPEdgeBlock(Base):
    def setUp(self):
        FakeMCP.seen = []
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeMCP)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.fake.server_address[1]}/mcp"
        super().setUp()

    def start(self):
        self.cfg.bm_mcp_url = self.url
        self.bl = os.path.join(self.tmp.name, "blocked.txt")
        with open(self.bl, "w") as fh:
            fh.write("# c\nwrite_note\nedit_note\ndelete_note\nmove_note\n")
        self.cfg.blocked_tools_file = self.bl
        super().start()

    def tearDown(self):
        self.fake.shutdown()
        self.fake.server_close()
        super().tearDown()

    def rpc(self, name, rid=1, token=TOKEN):
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                           "params": {"name": name, "arguments": {}}}).encode()
        return self.req("POST", "/mcp", body, {"Content-Type": "application/json"}, token=token)

    def test_requires_auth(self):
        self.assertEqual(self.rpc("read_note", token=None)[0], 401)
        self.assertEqual(FakeMCP.seen, [])

    def test_write_tools_refused_with_jsonrpc_error_and_not_forwarded(self):
        for t in ("write_note", "edit_note", "delete_note", "move_note"):
            s, d, _ = self.rpc(t, rid=7)
            self.assertEqual(s, 200)
            out = json.loads(d)
            self.assertEqual(out["id"], 7)
            self.assertEqual(out["error"]["code"], -32001)
            self.assertIn("shim REST verbs", out["error"]["message"])
            self.assertIn(t, out["error"]["message"])
        self.assertEqual(FakeMCP.seen, [], "blocked calls must never reach Basic Memory")

    def test_read_tools_and_other_methods_pass_through(self):
        for t in ("read_note", "search_notes", "build_context", "recent_activity", "view_note"):
            s, d, h = self.rpc(t)
            self.assertEqual(s, 200)
            self.assertIn(b'"ok": true', d)
        self.assertEqual(h.get("mcp-session-id"), "s9")
        self.req("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode())
        self.assertEqual(FakeMCP.seen[-1]["method"], "tools/list")
        self.assertEqual(len(FakeMCP.seen), 6)

    def test_batch_with_blocked_call_is_not_forwarded(self):
        b = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_note"}},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "write_note"}}]).encode()
        s, d, _ = self.req("POST", "/mcp", b, {"Content-Type": "application/json"})
        self.assertEqual(json.loads(d)[0]["id"], 2)
        self.assertEqual(FakeMCP.seen, [])

    def test_block_list_is_config_and_relaxable(self):
        with open(self.bl, "w") as fh:
            fh.write("edit_note\n")
        os.utime(self.bl, (time.time() + 5, time.time() + 5))
        self.assertIn(b'"ok": true', self.rpc("write_note")[1])
        self.assertIn("error", json.loads(self.rpc("edit_note")[1]))

    def test_missing_block_file_fails_closed(self):
        os.unlink(self.bl)
        self.assertIn("error", json.loads(self.rpc("write_note")[1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
