#!/usr/bin/env python3
"""vault-shim: single-writer, authenticated HTTP front for the markdown vault.

Why this exists (trial findings 2026-10-01):
  * Basic Memory (BM) edit_note/append loses ~85% of concurrent writes to one note
    and reports success. So NOTHING writes through BM. This service is the ONLY
    writer: every mutation takes a per-path lock, is applied to the file on disk
    atomically (temp file + fsync + rename + dir fsync), and BM's watcher indexes
    the result. BM MCP stays the read/search side.
  * BM stalls above ~5 MB per note -> writes that would take a note over
    MAX_NOTE_BYTES (default 2 MiB) are refused with a message telling the caller
    to roll to a new monthly file.

REST surface mirrors the Obsidian Local REST API verbs that scripts and hooks use:
  GET/HEAD /vault/<path>          read (HEAD -> ETag); dir (trailing /) -> {"files":[...]}
  PUT      /vault/<path>          write (create/replace)
  POST     /vault/<path>          append (404 if missing, like Obsidian; header
                                  X-Create-If-Missing: true creates instead)
  PATCH    /vault/<path>          Operation: append|prepend|replace
                                  Target-Type: heading|frontmatter  Target: <x> / <A::B>
  DELETE   /vault/<path>
  POST     /_move  /_copy         JSON {"from","to","overwrite":false}
  POST     /search/               JsonLogic (glob/regexp/var/...) over files (stat polling queries)
  POST     /search/simple/?query= proxied to BM search_notes over MCP (retrying)
  *        /mcp[/...]             authenticated proxy to BM MCP; tools/call of any tool in
                                  BLOCKED_TOOLS_FILE (write_note, edit_note, ...) is refused with a JSON-RPC error
  GET      /_auth                 204 if bearer ok
  GET      /_status               stale-flag + limits (authenticated)
  GET      /healthz               unauthenticated liveness

Auth: `Authorization: Bearer <token>`; token read from the file named by
VAULT_TOKEN_FILE (re-read when it changes), compared in constant time. The token
is never logged and never taken from argv. Access log = method, path, status only.
Stdlib only.
"""
import fnmatch
import hashlib
import http.client
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_MAX_NOTE_BYTES = 2 * 1024 * 1024
FAIL_RE = re.compile(r"modified concurrently|database is locked|database table is locked", re.I)


class ShimError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or status * 100


# --------------------------------------------------------------------------- config
class Config:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.root = Path(e.get("VAULT_ROOT", "/vault")).resolve()
        self.token_file = e.get("VAULT_TOKEN_FILE", "")
        self.bind = e.get("SHIM_BIND", "127.0.0.1")
        self.port = int(e.get("SHIM_PORT", "27123"))
        self.max_note_bytes = int(e.get("MAX_NOTE_BYTES", str(DEFAULT_MAX_NOTE_BYTES)))
        self.max_body_bytes = int(e.get("MAX_BODY_BYTES", str(self.max_note_bytes + 65536)))
        self.bm_mcp_url = e.get("BM_MCP_URL", "")
        self.bm_timeout = float(e.get("BM_TIMEOUT", "30"))
        self.state_dir = Path(e.get("STATE_DIR", "/state"))
        self.umask = int(e.get("VAULT_UMASK", "000"), 8)
        self.blocked_tools_file = e.get("BLOCKED_TOOLS_FILE", "/etc/vault-stack/blocked_tools.txt")
        self.settle_s = float(e.get("SETTLE_S", "6"))
        self.search_cap = int(e.get("SEARCH_RESULT_CAP", "200"))


# --------------------------------------------------------------------------- auth
class TokenAuth:
    """Bearer token from a file; constant-time compare; hot-reload on change."""

    def __init__(self, path):
        if not path:
            raise SystemExit("vault-shim: VAULT_TOKEN_FILE is required")
        self.path = path
        self._sig = None
        self._tok = b""
        self._load()
        if not self._tok:
            raise SystemExit(f"vault-shim: token file {path} is empty")

    def _load(self):
        st = os.stat(self.path)
        sig = (st.st_mtime_ns, st.st_size)
        if sig != self._sig:
            tok = Path(self.path).read_text(encoding="utf-8").strip()
            if tok.lower().startswith("bearer "):
                tok = tok[7:].strip()
            self._tok = tok.encode()
            self._sig = sig

    def check(self, header_value) -> bool:
        try:
            self._load()
        except OSError:
            return False
        if not self._tok:
            return False
        hv = (header_value or "").strip()
        if hv[:7].lower() == "bearer ":
            hv = hv[7:].strip()
        else:
            return False
        # hmac.compare_digest on equal-length digests: no length/early-exit timing leak
        a = hashlib.sha256(hv.encode()).digest()
        b = hashlib.sha256(self._tok).digest()
        return hmac.compare_digest(a, b)


# --------------------------------------------------------------------------- MCP edge block
class ToolBlocklist:
    """Names from a text file (one per line, # comments); re-read when the file changes.
    A missing/unreadable file blocks NOTHING only if BLOCKED_TOOLS_FILE is unset-by-design;
    otherwise we fail closed to the built-in default list."""
    DEFAULT = ("write_note", "edit_note", "delete_note", "move_note", "create_memory_project", "delete_project")

    def __init__(self, path):
        self.path, self._sig, self._names = path, None, set(self.DEFAULT)

    def names(self):
        try:
            st = os.stat(self.path)
            sig = (st.st_mtime_ns, st.st_size)
            if sig != self._sig:
                lines = Path(self.path).read_text(encoding="utf-8").splitlines()
                self._names = {l.strip() for l in lines if l.strip() and not l.strip().startswith("#")}
                self._sig = sig
        except OSError:
            self._names, self._sig = set(self.DEFAULT), None  # fail closed
        return self._names


BLOCK_MSG = ("Tool '{t}' is blocked at the vault edge: Basic Memory write tools lose concurrent writes while "
             "reporting success. Use the shim REST verbs instead (PUT/POST-append/PATCH/DELETE on /vault/<path>, "
             "POST /_move, /_copy; or a script that calls them). Reads/search via MCP are allowed.")


def mcp_block_response(body: bytes, blocked):
    """If the JSON-RPC body (single or batch) calls a blocked tool, return the JSON reply
    to send instead of forwarding, else None. Notifications (no id) cannot be answered."""
    try:
        msg = json.loads(body)
    except ValueError:
        return None
    batch = isinstance(msg, list)
    items = msg if batch else [msg]
    hit = [m for m in items if isinstance(m, dict) and m.get("method") == "tools/call"
           and str((m.get("params") or {}).get("name", "")) in blocked]
    if not hit:
        return None
    # A batch containing a blocked call is answered locally in full: nothing is forwarded to BM.
    errs = [{"jsonrpc": "2.0", "id": m.get("id"), "error": {
        "code": -32001, "message": BLOCK_MSG.format(t=m["params"]["name"]),
        "data": {"blocked_tool": m["params"]["name"], "use": "shim REST verbs"}}} for m in hit if "id" in m]
    return errs if batch else (errs[0] if errs else {})


# --------------------------------------------------------------------------- locks
class LockTable:
    """Per-key mutex with refcounted cleanup. Multi-key acquisition is ordered."""

    def __init__(self):
        self._g = threading.Lock()
        self._locks = {}

    def acquire(self, *keys):
        keys = sorted(set(keys))
        held = []
        for k in keys:
            with self._g:
                ent = self._locks.setdefault(k, [threading.Lock(), 0])
                ent[1] += 1
            ent[0].acquire()
            held.append(k)
        return held

    def release(self, held):
        for k in reversed(held):
            with self._g:
                ent = self._locks[k]
                ent[0].release()
                ent[1] -= 1
                if ent[1] == 0:
                    del self._locks[k]

    def __call__(self, *keys):
        table = self

        class _Ctx:
            def __enter__(self_):
                self_.h = table.acquire(*keys)

            def __exit__(self_, *a):
                table.release(self_.h)

        return _Ctx()


# --------------------------------------------------------------------------- store
def etag_of(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'


def next_roll_name(rel: str) -> str:
    """Suggest the next monthly file name for an over-cap note."""
    m = re.search(r"(\d{4})-(\d{2})(\.md)$", rel)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        return rel[: m.start()] + f"{y:04d}-{mo:02d}.md"
    now = time.gmtime()
    stem = rel[:-3] if rel.endswith(".md") else rel
    return f"{stem}-{now.tm_year:04d}-{now.tm_mon:02d}.md"


class Store:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.root
        self.locks = LockTable()
        self._settle = {}
        self._settle_g = threading.Lock()
        os.umask(cfg.umask)

    def schedule_settle(self, rel):
        """BM's polling watcher can index an intermediate version during a burst of writes and
        never notice the final one (seen ~1 in 8 runs on a 20-way append test). After SETTLE_S
        of quiet on a path we bump its mtime (content untouched) so the next poll re-checks it."""
        if self.cfg.settle_s <= 0:
            return
        with self._settle_g:
            old = self._settle.pop(rel, None)
            if old:
                old.cancel()
            t = threading.Timer(self.cfg.settle_s, self._do_settle, (rel,))
            t.daemon = True
            self._settle[rel] = t
            t.start()

    def _do_settle(self, rel):
        with self._settle_g:
            self._settle.pop(rel, None)
        try:
            with self.locks(rel):
                os.utime(self.resolve(rel), None)
        except (OSError, ShimError):
            pass

    # ---- paths
    def resolve(self, rel: str, *, allow_dir=False) -> Path:
        rel = (rel or "").lstrip("/")
        if "\x00" in rel or "\\" in rel:
            raise ShimError(400, "invalid path")
        parts = [p for p in rel.split("/") if p != ""]
        if any(p in (".", "..") or p.startswith(".") for p in parts):
            raise ShimError(400, "path may not contain '..' or hidden segments")
        if not parts and not allow_dir:
            raise ShimError(400, "empty path")
        p = self.root.joinpath(*parts)
        # symlink escape: resolve the deepest existing ancestor and demand containment
        probe = p
        while not probe.exists() and probe != self.root:
            probe = probe.parent
        try:
            probe.resolve().relative_to(self.root)
        except ValueError:
            raise ShimError(400, "path escapes vault")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    # ---- primitives
    @staticmethod
    def _fsync_dir(d: Path):
        try:
            fd = os.open(str(d), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _atomic_write(self, p: Path, data: bytes):
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f".{p.name}.{secrets.token_hex(6)}.tmp"
        mode = None
        try:
            mode = p.stat().st_mode & 0o7777
        except OSError:
            pass
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if mode is not None:
                os.chmod(tmp, mode)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._fsync_dir(p.parent)

    def _guard_size(self, rel, old_size, new_size):
        cap = self.cfg.max_note_bytes
        if new_size > cap and new_size > old_size:  # shrinking an oversize note is allowed
            raise ShimError(
                413,
                f"SIZE GUARD: {rel} would be {new_size} bytes (cap {cap}, currently {old_size}). "
                f"Basic Memory stalls on big notes: roll to a new file, e.g. {next_roll_name(rel)}",
                code=41301,
            )

    def _read_for_write(self, p: Path):
        try:
            return p.read_bytes()
        except FileNotFoundError:
            return None
        except IsADirectoryError:
            raise ShimError(400, "path is a directory")

    @staticmethod
    def _check_if_match(cur, if_match, if_none_match):
        if if_none_match == "*" and cur is not None:
            raise ShimError(412, "If-None-Match: * but note exists")
        if if_match:
            if cur is None or etag_of(cur) != if_match.strip():
                raise ShimError(412, "If-Match failed (note changed since you read it)")

    # ---- verbs
    def read(self, rel):
        p = self.resolve(rel)
        try:
            return p.read_bytes(), p.stat()
        except FileNotFoundError:
            raise ShimError(404, "not found")
        except IsADirectoryError:
            raise ShimError(400, "is a directory; use trailing /")

    def stat(self, rel):
        p = self.resolve(rel)
        try:
            data = p.read_bytes()
            return etag_of(data), p.stat()
        except (FileNotFoundError, IsADirectoryError):
            raise ShimError(404, "not found")

    def listdir(self, rel):
        p = self.resolve(rel, allow_dir=True)
        if not p.is_dir():
            raise ShimError(404, "not found")
        names = []
        for c in sorted(p.iterdir()):
            if c.name.startswith("."):
                continue
            names.append(c.name + "/" if c.is_dir() else c.name)
        return {"files": names}

    def write(self, rel, data: bytes, if_match=None, if_none_match=None):
        p = self.resolve(rel)
        with self.locks(rel):
            cur = self._read_for_write(p)
            self._check_if_match(cur, if_match, if_none_match)
            self._guard_size(rel, len(cur or b""), len(data))
            self._atomic_write(p, data)
        self.schedule_settle(rel)
        return etag_of(data)

    def append(self, rel, data: bytes, create=False, if_match=None):
        p = self.resolve(rel)
        with self.locks(rel):
            cur = self._read_for_write(p)
            if cur is None and not create:
                raise ShimError(404, "not found")
            self._check_if_match(cur, if_match, None)
            base = cur or b""
            sep = b"\n" if base and not base.endswith(b"\n") and not data.startswith(b"\n") else b""
            new = base + sep + data
            self._guard_size(rel, len(base), len(new))
            self._atomic_write(p, new)
        self.schedule_settle(rel)
        return etag_of(new)

    def delete(self, rel, if_match=None):
        p = self.resolve(rel)
        with self.locks(rel):
            cur = self._read_for_write(p)
            if cur is None:
                raise ShimError(404, "not found")
            self._check_if_match(cur, if_match, None)
            os.unlink(p)
            self._fsync_dir(p.parent)

    def move(self, src, dst, overwrite=False):
        ps, pd = self.resolve(src), self.resolve(dst)
        with self.locks(src, dst):
            if not ps.is_file():
                raise ShimError(404, "source not found")
            if pd.exists() and not overwrite:
                raise ShimError(409, "destination exists (overwrite=false)")
            pd.parent.mkdir(parents=True, exist_ok=True)
            os.replace(ps, pd)
            self._fsync_dir(pd.parent)
            self._fsync_dir(ps.parent)
        self.schedule_settle(dst)

    def copy(self, src, dst, overwrite=False):
        ps, pd = self.resolve(src), self.resolve(dst)
        with self.locks(src, dst):
            data = self._read_for_write(ps)
            if data is None:
                raise ShimError(404, "source not found")
            if pd.exists() and not overwrite:
                raise ShimError(409, "destination exists (overwrite=false)")
            self._guard_size(dst, len(self._read_for_write(pd) or b""), len(data))
            self._atomic_write(pd, data)
        self.schedule_settle(dst)

    # ---- PATCH
    def patch(self, rel, body: bytes, operation, target_type, target, if_match=None):
        p = self.resolve(rel)
        operation = (operation or "").lower()
        target_type = (target_type or "").lower()
        if operation not in ("append", "prepend", "replace"):
            raise ShimError(400, "Operation header must be append|prepend|replace")
        text_body = body.decode("utf-8", "replace")
        with self.locks(rel):
            cur = self._read_for_write(p)
            if cur is None:
                raise ShimError(404, "not found")
            self._check_if_match(cur, if_match, None)
            text = cur.decode("utf-8", "replace")
            if target_type == "heading":
                new = patch_heading(text, target, operation, text_body)
            elif target_type == "frontmatter":
                new = patch_frontmatter(text, target, operation, text_body)
            else:
                raise ShimError(501, f"Target-Type {target_type!r} not supported (heading, frontmatter only)")
            nb = new.encode("utf-8")
            self._guard_size(rel, len(cur), len(nb))
            self._atomic_write(p, nb)
        self.schedule_settle(rel)
        return etag_of(nb)

    # ---- walking for search
    def walk_md(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                if fn.startswith(".") or not fn.endswith(".md"):
                    continue
                yield Path(dirpath) / fn


def patch_heading(text, target, operation, body):
    if not target:
        raise ShimError(400, "Target header required")
    want = [t.strip() for t in target.split("::")]
    lines = text.split("\n")
    stack, start, level = [], None, None
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        lvl, title = len(m.group(1)), m.group(2).strip()
        if start is not None and lvl <= level:
            end = i
            break
        stack = [s for s in stack if s[0] < lvl] + [(lvl, title)]
        if start is None and [s[1] for s in stack][-len(want):] == want and len(stack) >= len(want):
            start, level = i, lvl
    else:
        end = len(lines)
    if start is None:
        raise ShimError(404, f"heading {target!r} not found")
    sec = lines[start + 1:end]
    # keep trailing blank lines (separator before next heading) out of the way
    trail = 0
    while sec and sec[-1].strip() == "":
        sec.pop()
        trail += 1
    new_body = body.strip("\n").split("\n") if body.strip("\n") else []
    if operation == "append":
        sec = sec + new_body
    elif operation == "prepend":
        sec = new_body + sec
    else:
        sec = [""] + new_body if new_body else []
    if trail == 0 and end < len(lines):
        trail = 1
    out = lines[: start + 1] + sec + [""] * trail + lines[end:]
    return "\n".join(out)


def patch_frontmatter(text, target, operation, body):
    if operation == "append":
        raise ShimError(400, "frontmatter supports Operation: replace only")
    if not target:
        raise ShimError(400, "Target header required")
    try:
        val = json.loads(body)
        val = val if isinstance(val, str) else json.dumps(val)
    except ValueError:
        val = body.strip()
    line_new = f"{target}: {val}"
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            block = text[4:end].split("\n")
            for i, l in enumerate(block):
                if re.match(rf"^{re.escape(target)}\s*:", l):
                    block[i] = line_new
                    break
            else:
                block.append(line_new)
            return "---\n" + "\n".join(block) + text[end:]
    return f"---\n{line_new}\n---\n{text}"


# --------------------------------------------------------------------------- JsonLogic subset
def glob_to_re(pat: str):
    out, i = "", 0
    while i < len(pat):
        c = pat[i]
        if pat[i:i + 3] == "**/":
            out += "(?:.*/)?"
            i += 3
        elif pat[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile("^" + out + "$")


class LazyDoc(dict):
    def __init__(self, store, p, rel):
        st = p.stat()
        super().__init__(path=rel, stat={"ctime": int(st.st_ctime * 1000), "mtime": int(st.st_mtime * 1000),
                                         "size": st.st_size}, tags=[], frontmatter={})
        self._p = p
        self._loaded = False

    def __missing__(self, key):
        if key == "content":
            try:
                v = self._p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                v = ""
            self["content"] = v
            return v
        raise KeyError(key)


def _var(data, name):
    cur = data
    for part in str(name).split(".") if name != "" else []:
        try:
            cur = cur[part]
        except (KeyError, TypeError, IndexError):
            return None
    return cur


def jl(expr, data):
    if isinstance(expr, list):
        return [jl(e, data) for e in expr]
    if not isinstance(expr, dict) or len(expr) != 1:
        return expr
    op, args = next(iter(expr.items()))
    if not isinstance(args, list):
        args = [args]
    if op == "var":
        return _var(data, jl(args[0], data) if args else "")
    if op == "if":
        i = 0
        while i + 1 < len(args):
            if jl(args[i], data):
                return jl(args[i + 1], data)
            i += 2
        return jl(args[i], data) if i < len(args) else None
    if op == "or":
        r = None
        for a in args:
            r = jl(a, data)
            if r:
                return r
        return r
    if op == "and":
        r = True
        for a in args:
            r = jl(a, data)
            if not r:
                return r
        return r
    vals = [jl(a, data) for a in args]
    if op == "!":
        return not vals[0]
    if op in ("==", "==="):
        return vals[0] == vals[1]
    if op in ("!=", "!=="):
        return vals[0] != vals[1]
    if op == "in":
        return vals[0] in (vals[1] or [])
    if op == "glob":
        return bool(glob_to_re(str(vals[0])).match(str(vals[1] or "")))
    if op == "regexp":
        flags = re.I if len(vals) > 2 and "i" in str(vals[2]) else 0
        return bool(re.search(str(vals[0]), str(vals[1] or ""), flags))
    if op in (">", "<", ">=", "<="):
        a, b = vals[0], vals[1]
        try:
            return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]
        except TypeError:
            return False
    raise ShimError(400, f"unsupported JsonLogic operator {op!r}")


def jsonlogic_search(store: Store, query, cap):
    out = []
    for p in store.walk_md():
        rel = store.rel(p)
        try:
            r = jl(query, LazyDoc(store, p, rel))
        except OSError:
            continue
        if r:
            out.append({"filename": rel, "result": r})
            if len(out) >= cap:
                break
    return out


# --------------------------------------------------------------------------- BM MCP client
class BMError(Exception):
    def __init__(self, msg, retryable=False):
        super().__init__(msg)
        self.retryable = retryable


# Loopback calls must NEVER go through a proxy: a stray HTTP_PROXY/ALL_PROXY (Docker/Unraid can inject
# one) would make urllib resolve the proxy host instead of talking to 127.0.0.1.
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BMClient:
    """Minimal MCP streamable-http client (initialize / tools/call).

    The BM tools return is_error=False with failure TEXT for races; FAIL_RE texts are
    therefore treated as failures (retried, then raised), never as success."""

    def __init__(self, url, timeout=30.0, retries=4, backoff=0.4):
        self.url, self.timeout, self.retries, self.backoff = url, timeout, retries, backoff
        self._sid = None
        self._id = 0
        self._lock = threading.Lock()

    def _post(self, payload, sid=None):
        hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if sid:
            hdr["mcp-session-id"] = sid
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=hdr, method="POST")
        with _LOOPBACK_OPENER.open(req, timeout=self.timeout) as r:
            body = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("Content-Type", "")
            return r.headers.get("mcp-session-id"), ctype, body

    @staticmethod
    def _parse(ctype, body, want_id):
        if not body.strip():
            return None
        if "text/event-stream" in ctype:
            found = None
            for line in body.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if want_id is None or msg.get("id") == want_id:
                        found = msg
            return found
        return json.loads(body)

    def _init(self):
        self._id += 1
        sid, ctype, body = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "vault-shim", "version": "1"}}})
        msg = self._parse(ctype, body, self._id)
        if not msg or "error" in msg:
            raise BMError(f"initialize failed: {msg}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        self._sid = sid

    def call(self, tool, args):
        last = None
        for attempt in range(self.retries + 1):
            try:
                return self._call_once(tool, args)
            except BMError as e:
                last = e
                if not e.retryable:
                    raise
            time.sleep(self.backoff * (2 ** attempt))
        raise BMError(f"{tool}: still failing after {self.retries} retries: {last}")

    def _call_once(self, tool, args):
        with self._lock:
            for fresh in (False, True):
                try:
                    if self._sid is None or fresh:
                        self._init()
                    self._id += 1
                    rid = self._id
                    _, ctype, body = self._post({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                                 "params": {"name": tool, "arguments": args}}, self._sid)
                    msg = self._parse(ctype, body, rid)
                    break
                except urllib.error.HTTPError as e:
                    self._sid = None
                    if fresh or e.code not in (400, 404, 409):
                        raise BMError(f"BM HTTP {e.code}", retryable=e.code >= 500)
                except (urllib.error.URLError, OSError, TimeoutError) as e:
                    self._sid = None
                    raise BMError(f"BM unreachable ({self.url}): {type(e).__name__}: {e}", retryable=True)
        if not msg:
            raise BMError("empty BM response", retryable=True)
        if "error" in msg:
            text = json.dumps(msg["error"])
            raise BMError(text, retryable=bool(FAIL_RE.search(text)))
        res = msg.get("result", {})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
        if FAIL_RE.search(text):
            raise BMError(f"BM reported a concurrency failure as text: {text[:200]}", retryable=True)
        if res.get("isError"):
            raise BMError(f"BM error: {text[:300]}", retryable=False)
        return {"text": text, "structured": res.get("structuredContent")}


def bm_search_results(res, store: Store, cap):
    """Normalize BM search_notes output to Obsidian /search/simple/ shape."""
    data = res.get("structured")
    if isinstance(data, dict) and "result" in data and isinstance(data["result"], dict):
        data = data["result"]
    if not isinstance(data, dict):
        try:
            data = json.loads(res.get("text") or "")
        except ValueError:
            data = None
    items = (data or {}).get("results") if isinstance(data, dict) else None
    out = []
    for it in items or []:
        fn = it.get("file_path") or it.get("permalink") or it.get("title") or ""
        ctx = it.get("content") or it.get("matched_chunk") or it.get("title") or ""
        out.append({"filename": fn, "score": it.get("score"), "title": it.get("title"),
                    "matches": [{"context": str(ctx)[:400]}]})
        if len(out) >= cap:
            break
    return out


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vault-shim"
    sys_version = ""

    # injected on the server object
    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):  # never log headers/query
        pass

    def _log(self, status):
        path = self.path.split("?", 1)[0]
        sys.stderr.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {self.command} {path} {status}\n")

    def _send(self, status, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._log(status)

    def _err(self, e: ShimError):
        self._send(e.status, {"errorCode": e.code, "message": e.message})

    def _read_body(self):
        cap = self.app.cfg.max_body_bytes
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks, total = [], 0
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0] or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    break
                total += size
                if total > cap:
                    raise ShimError(413, f"body exceeds {cap} bytes")
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        n = int(self.headers.get("Content-Length") or 0)
        if n > cap:
            self.close_connection = True
            raise ShimError(413, f"body of {n} bytes exceeds {cap}; roll to a new note", code=41301)
        return self.rfile.read(n) if n else b""

    def _authed(self):
        return self.app.auth.check(self.headers.get("Authorization"))

    def _dispatch(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if path == "/healthz":
                return self._send(200, {"ok": True})
            if not self._authed():
                return self._send(401, {"errorCode": 40101, "message": "unauthorized"},
                                  headers={"WWW-Authenticate": "Bearer"})
            m = self.command
            if path == "/_auth":
                return self._send(204)
            if path == "/_status":
                return self._send(200, self.app.status())
            if path.startswith("/vault/") or path == "/vault":
                return self._vault(m, path[len("/vault/"):] if path != "/vault" else "")
            if path == "/mcp" or path.startswith("/mcp/"):
                return self._mcp_proxy(m, parsed)
            if path in ("/_move", "/_copy") and m == "POST":
                spec = json.loads(self._read_body() or b"{}")
                fn = self.app.store.move if path == "/_move" else self.app.store.copy
                fn(spec.get("from", ""), spec.get("to", ""), bool(spec.get("overwrite")))
                return self._send(200, {"ok": True})
            if path in ("/search", "/search/") and m == "POST":
                body = self._read_body()
                try:
                    q = json.loads(body)
                except ValueError:
                    raise ShimError(400, "search body must be JsonLogic JSON")
                return self._send(200, jsonlogic_search(self.app.store, q, self.app.cfg.search_cap))
            if path.startswith("/search/simple") and m in ("POST", "GET"):
                query = (qs.get("query") or [""])[0]
                if not query:
                    raise ShimError(400, "query parameter required")
                return self._send(200, self.app.bm_search(query))
            raise ShimError(404, "no such endpoint")
        except ShimError as e:
            self._err(e)
        except BMError as e:
            self._err(ShimError(502, f"Basic Memory: {e}", code=50200))
        except (ValueError, UnicodeError) as e:
            self._err(ShimError(400, f"bad request: {e}"))
        except Exception as e:  # never leak a traceback with content to the client
            sys.stderr.write(f"internal error: {type(e).__name__}: {e}\n")
            self._err(ShimError(500, "internal error"))

    def _mcp_proxy(self, m, parsed):
        """Authenticated pass-through to BM's MCP endpoint that refuses blocked tools/call."""
        if not self.app.cfg.bm_mcp_url:
            raise ShimError(501, "BM_MCP_URL not configured")
        base = urllib.parse.urlsplit(self.app.cfg.bm_mcp_url)
        body = self._read_body() if m in ("POST", "PUT", "DELETE") else b""
        if m == "POST":
            blocked = mcp_block_response(body, self.app.blocklist.names())
            if blocked is not None:
                sys.stderr.write("mcp: blocked tools/call (write tool)\n")
                if blocked == {} or blocked == []:  # notification: nothing to answer
                    return self._send(202)
                return self._send(200, blocked)
        tail = parsed.path[len("/mcp"):]
        target = base.path.rstrip("/") + tail + (("?" + parsed.query) if parsed.query else "")
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() in ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")}
        conn = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=600)
        try:
            conn.request(m, target, body=body or None, headers=hdrs)
            r = conn.getresponse()
            ctype = r.getheader("Content-Type", "application/json")
            extra = {k: v for k, v in r.getheaders() if k.lower() in ("mcp-session-id", "cache-control")}
            if m == "GET" and "text/event-stream" in ctype:  # long-lived stream: relay until either side closes
                self.send_response(r.status)
                self.send_header("Content-Type", ctype)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    chunk = r.read1(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return self._log(r.status)
            data = r.read()
            return self._send(r.status, data, ctype, extra)
        except OSError as e:
            raise ShimError(502, f"Basic Memory MCP unreachable: {e}", code=50200)
        finally:
            conn.close()

    def _vault(self, m, rel):
        store = self.app.store
        h = self.headers
        if rel == "" or rel.endswith("/"):
            if m not in ("GET", "HEAD"):
                raise ShimError(405, "directories are read-only")
            return self._send(200, store.listdir(rel))
        if m in ("GET", "HEAD"):
            if m == "HEAD":
                et, st = store.stat(rel)
                return self._send(200, b"", "text/markdown", {"ETag": et, "X-Content-Length": str(st.st_size)})
            data, st = store.read(rel)
            if "application/vnd.olrapi.note+json" in (h.get("Accept") or ""):
                return self._send(200, {"path": rel, "content": data.decode("utf-8", "replace"), "tags": [],
                                        "frontmatter": {}, "stat": {"ctime": int(st.st_ctime * 1000),
                                                                    "mtime": int(st.st_mtime * 1000),
                                                                    "size": st.st_size}},
                                  "application/vnd.olrapi.note+json", {"ETag": etag_of(data)})
            return self._send(200, data, "text/markdown; charset=utf-8", {"ETag": etag_of(data)})
        if m == "PUT":
            et = store.write(rel, self._read_body(), h.get("If-Match"), h.get("If-None-Match"))
            return self._send(204, headers={"ETag": et})
        if m == "POST":
            create = (h.get("X-Create-If-Missing") or "").lower() == "true"
            et = store.append(rel, self._read_body(), create=create, if_match=h.get("If-Match"))
            return self._send(204, headers={"ETag": et})
        if m == "PATCH":
            et = store.patch(rel, self._read_body(), h.get("Operation"), h.get("Target-Type"),
                             urllib.parse.unquote(h.get("Target") or ""), h.get("If-Match"))
            return self._send(200, {"ok": True}, headers={"ETag": et})
        if m == "DELETE":
            store.delete(rel, h.get("If-Match"))
            return self._send(204)
        raise ShimError(405, "method not allowed")

    do_GET = do_HEAD = do_PUT = do_POST = do_PATCH = do_DELETE = _dispatch


class App:
    def __init__(self, cfg: Config, bm=None):
        self.cfg = cfg
        self.store = Store(cfg)
        self.auth = TokenAuth(cfg.token_file)
        self.blocklist = ToolBlocklist(cfg.blocked_tools_file)
        self.bm = bm if bm is not None else (BMClient(cfg.bm_mcp_url, cfg.bm_timeout) if cfg.bm_mcp_url else None)

    def bm_search(self, query):
        if self.bm is None:
            raise ShimError(501, "BM_MCP_URL not configured")
        res = self.bm.call("search_notes", {"query": query, "page_size": min(self.cfg.search_cap, 50),
                                            "output_format": "json"})
        return bm_search_results(res, self.store, self.cfg.search_cap)

    def status(self):
        flag = None
        try:
            flag = json.loads((self.cfg.state_dir / "STALE").read_text())
        except (OSError, ValueError):
            pass
        return {"ok": True, "max_note_bytes": self.cfg.max_note_bytes, "stale": flag}


class _Server(ThreadingHTTPServer):
    request_queue_size = 256  # default 5 resets connections under bursts of concurrent writers


def make_server(cfg: Config, bm=None):
    app = App(cfg, bm)
    srv = _Server((cfg.bind, cfg.port), Handler)
    srv.daemon_threads = True
    srv.app = app
    return srv


def main():
    cfg = Config()
    if not cfg.root.is_dir():
        raise SystemExit(f"vault-shim: VAULT_ROOT {cfg.root} is not a directory")
    srv = make_server(cfg)
    sys.stderr.write(f"vault-shim listening on {cfg.bind}:{cfg.port} root={cfg.root} "
                     f"cap={cfg.max_note_bytes}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
