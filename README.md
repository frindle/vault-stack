# vault-stack

**An Obsidian-Local-REST-compatible API plus an authenticated MCP endpoint over a plain markdown folder, in a single container.**
[Basic Memory](https://github.com/basicmachines-co/basic-memory) 0.23.2 indexes and searches the notes; a small single-writer shim does
every write (atomically, with locking and a size cap); a committer keeps the folder in git with a backup remote and a staleness alarm.
It was built for Unraid (own LAN IP on the `br0` macvlan) but is plain Docker Compose.

* Claude Code, Goose and any MCP client use `http://<VAULT_IP>:27123/mcp` (bearer token; Basic Memory's write tools are refused, see below).
* Scripts use the REST verbs (`GET/PUT/POST-append/PATCH/DELETE /vault/<path>`, `/search/`), the same shape as the Obsidian Local REST API.
* Notes stay plain `.md` files you can also edit over SMB/Obsidian; nothing is stored in a database you cannot rebuild.

```
 MCP clients / scripts ──> :27123 shim ──(/mcp: token + write tools blocked)──> 127.0.0.1:8000 Basic Memory
   (bearer token)          atomic writes, size guard                            (no auth; loopback only; polls, indexes)
                                |                 committer (60 s, debounced git)
                                v                          |
                  <vault folder>  (plain .md + .git)  <────┘     supervisor: any child dies -> exit != 0
                  ^ you can edit it over SMB too                  -> Docker restarts the container
```

Why this shape: Basic Memory's default config rewrites ~90% of files on first sync, concurrent BM `edit_note` calls lose ~85% of writes while
reporting success, and BM stalls on notes over ~5 MB. So: a safe config + drift guard, **nothing writes through BM** (the shim writes files
atomically under a per-path lock; BM just indexes them ~1-2 s later), a 2 MB per-note cap, and BM never faces the network.

## Layout
| Path | What |
|---|---|
| `docker-compose.yml`, `.env.example`, `Dockerfile` | ONE service `vault` on `br0` (IP/MAC from `.env`), one image (`basic-memory==0.23.2` + stdlib Python + git) |
| `container/supervisor.py`, `container/healthcheck.py` | PID 1: starts BM / shim / committer, exits non-zero if ANY dies; HEALTHCHECK = shim + BM + committer heartbeat |
| `config/config.json` | the safe BM config (seeded on first boot) |
| `config/blocked_tools.txt` | BM MCP tools refused at the edge (editable) |
| `bm/entrypoint.sh`, `bm/drift_guard.py`, `bm/make_bmignore.py` | drift guard, oversize-file ignore |
| `shim/vault_shim.py` + `shim/tests/` | REST writer (30 tests) |
| `committer/committer.py`, `committer/staleness.py` + tests | commit/push loop, staleness alarm (16 tests) |
| `scripts/prepare.sh` | one-shot host prep: .env, folders, token, ownership |
| `tests/` | drift-guard (9) + supervisor (4) tests, `revert_tests.py`, `integration_bm.py` (real BM), `supervised_check.py` (real supervisor + BM + shim + committer, no docker), `run_all.sh` |

## Where things live (Unraid)
| What | Path | Why |
|---|---|---|
| This repo (compose, `.env`, `secrets/vault_token`) | `/mnt/user/appdata/vault-stack` | app config, `git clone`d here and updated with `git pull` |
| Basic Memory config, SQLite index, `.bmignore`, committer state, local backup repo | `/mnt/user/appdata/basic-memory` | rebuildable runtime data |
| **The notes (vault folder)** | `/mnt/user/data/Documents/Vault` (any folder; set `VAULT_DIR`) | **your data**, so it stays on the data share; mount ONLY this folder, never its parent |

## Install on Unraid
Quick path (root shell on the Unraid host):
```sh
git clone <this-repo-url> /mnt/user/appdata/vault-stack && cd /mnt/user/appdata/vault-stack
./scripts/prepare.sh        # creates .env, the folders, secrets/vault_token (never printed) and chowns everything to 99:100
nano .env                   # set VAULT_IP and VAULT_MAC (see step 3)
docker compose up -d --build
```
`prepare.sh` is safe to re-run. The same steps by hand:
1. **Folders, owned by the container user (`PUID:PGID`, default 99:100), BEFORE the first `docker compose up`**
   ```sh
   mkdir -p /mnt/user/appdata/basic-memory/state /mnt/user/appdata/basic-memory/backup /mnt/user/data/Documents/Vault
   chown -R 99:100 /mnt/user/appdata/basic-memory /mnt/user/data/Documents/Vault
   ```
2. **Clone the stack into appdata**: `git clone <this-repo-url> /mnt/user/appdata/vault-stack && cd /mnt/user/appdata/vault-stack`
3. **Token** (never committed; `secrets/` is git-ignored). Create it BEFORE the first `docker compose up`, or Docker makes a *directory* at that path:
   ```sh
   mkdir -p secrets && (umask 077; openssl rand -hex 32 > secrets/vault_token) && chown 99:100 secrets/vault_token && chmod 400 secrets/vault_token
   ```
   The container runs as 99:100, so the file must be readable by that uid (a root-owned 0600 file is not). Give the same token to your
   clients (e.g. a file in `~/.config/` with mode 600). Never put it in argv or a repo.
4. **Import your notes** into the vault folder *before* first start (see "Importing an existing vault").
5. **Configure.** Confirm the macvlan exists: `docker network ls | grep br0`. Then `cp .env.example .env` and set `VAULT_IP` / `VAULT_MAC`
   (compose refuses to start without them): a free address on your LAN (check with `ping` and `arp -a`; it must not be in your DHCP range) and a
   unique locally-administered MAC (first byte `02`, e.g. `02:xx:xx:xx:xx:xx`). `VAULT_URL` for clients is `http://<VAULT_IP>:27123`.
6. **Start**
   ```sh
   docker compose up -d --build
   docker compose ps                 # "vault" Up, healthy after ~2-3 min
   docker logs vault | head -30      # must show "drift guard: config ok" and 3 supervisor "started" lines
   ```
   If a token or folder is not usable by the container's uid, the supervisor refuses to start and logs `REFUSING TO START: ... FIX: <exact command>`.
7. **Check** from ANOTHER machine on the LAN (the Unraid host itself cannot reach a macvlan container's IP):
   ```sh
   T=$(cat ~/.config/vault/token); U=http://<VAULT_IP>:27123     # your token file / IP
   curl -s $U/healthz
   curl -s -H "Authorization: Bearer $T" $U/_status
   curl -s -X POST -H "Authorization: Bearer $T" "$U/search/simple/?query=hello"
   ```
   On Unraid itself use `docker exec vault python3 /app/healthcheck.py && echo healthy`, and:
   ```sh
   git -C /mnt/user/data/Documents/Vault log --oneline | head -3        # 'vault: auto ...' within ~2 min
   git -C /mnt/user/data/Documents/Vault status --porcelain | wc -l     # must be 0: BM rewrote nothing
   ```
8. **Update**: `cd /mnt/user/appdata/vault-stack && git pull && docker compose up -d --build`.

## Client config
Claude Code (use `/mcp` with **no** trailing slash):
```sh
claude mcp add --transport http vault http://<VAULT_IP>:27123/mcp --header "Authorization: Bearer $(cat ~/.config/vault/token)"
```
Tools: `read_note`, `search_notes`, `build_context`, `recent_activity`, ... (Basic Memory's write tools are refused; write via REST.)

Goose (`~/.config/goose/config.yaml`):
```yaml
extensions:
  vault:
    enabled: true
    name: vault
    type: streamable_http
    uri: http://<VAULT_IP>:27123/mcp
    timeout: 300
    headers:
      Authorization: "Bearer ${VAULT_TOKEN}"   # export VAULT_TOKEN in a wrapper; if header interpolation is not honoured, use the literal value and keep the file private
```

## REST surface (shim) - Obsidian Local REST API compatible, :27123
`GET/HEAD /vault/<path>` (HEAD -> `ETag`), `PUT` write, `POST` append (**404 if missing**, like Obsidian, so a client can
PUT-on-404 to create, e.g. a monthly log roll; `X-Create-If-Missing: true` creates), `PATCH` (`Operation: append|prepend|replace`,
`Target-Type: heading|frontmatter`, `Target: A::B`), `DELETE`, `GET /vault/dir/` -> `{"files":[...]}`,
`POST /_move` and `/_copy` `{"from","to","overwrite"}`, `POST /search/` (JsonLogic `glob/regexp/var/and/or/if/...`, the query shape clients use to
poll mtimes: `{"if":[{"glob":["Claude/**",{"var":"path"}]},{"var":"stat"},false]}` returning `[{filename,result:{ctime,mtime,size}}]`), `POST /search/simple/?query=` (proxied to BM
`search_notes`, retried, concurrency texts are failures), `GET /_status` (includes the staleness flag), `GET /healthz`.
Optional `If-Match` / `If-None-Match: *` for optimistic writes. Bearer token only (also on `/mcp`); token file hot-reloads (rotate = rewrite file).
Not supported: PATCH `Target-Type: block` (501).

## MCP write-tool block
`/mcp` is served by the shim, which authenticates and proxies to BM but answers any `tools/call` whose tool is listed in
`config/blocked_tools.txt` (default: write_note, edit_note, delete_note, move_note, create_memory_project, delete_project) with a JSON-RPC
error (code -32001, "use the shim REST verbs ...") and never forwards it (batches containing one are answered locally). Read tools
(read_note, search_notes, build_context, recent_activity, view_note, list_directory, read_content, schema_*) pass. The file is mounted read-only
into the shim and re-read on change; delete a line to relax; a missing file fails closed to the default list. `tools/list` is not filtered,
so clients still see the write tools but get the error when calling them. Basic Memory itself listens on 127.0.0.1:8000 inside the container, so the shim's `/mcp` is the only way in.

## Guards (and how they were proven to bite: `python3 tests/revert_tests.py`)
* **Drift guard** (`bm/drift_guard.py`, run by `entrypoint.sh` at start and every 60 s): refuses to run BM (exit 78, loud message,
  throttled by `DRIFT_EXIT_DELAY`) unless `ensure_frontmatter_on_sync=false, disable_permalinks=true, semantic_search_enabled=false,
  format_on_save=false, update_permalinks_on_move=false, kebab_filenames=false, auto_update=false` and the single project `main` ->
  `/vault`. Recover: `cp config/config.json /mnt/user/appdata/basic-memory/config.json` and restart. (`auto_update=false` and
  `cloud_promo_opt_out` are additions to the trial's list: self-update inside a pinned image is wrong.)
* **Size guard** (shim): appends/PUT/PATCH that would take a note over `MAX_NOTE_BYTES` (2 MiB) get HTTP 413 with
  `SIZE GUARD: ... roll to a new file, e.g. Claude/Activity-Log-2026-11.md`; shrinking an already-oversize note is allowed.
  Files that are already >2 MB (arrive via SMB/migration) are listed in a managed block of `.bmignore` at every BM start
  (BM can ignore by name only, not size): they stay in git/disk but are not indexed. Re-run by restarting the container.
* **Committer**: every 60 s, if dirty and the newest change is >=15 s old: `git add -A && git commit -m "vault: auto <UTC>"`
  (core.filemode=false, `.*.tmp` excluded, stale `index.lock` (>10 min, no git process) removed). `COMMITTER_REMOTE=/backup/vault.git`
  (auto-created bare repo) pushes `HEAD:main` after each new commit; failures retry next cycle.
* **Staleness alarm** (`committer/staleness.py`, runs each committer cycle; result at `/_status`): exit 1 and writes
  `state/STALE` (visible at `/_status`) when HEAD > 6 h old while dirty, committer heartbeat > 15 min old / missing, or HEAD unpushed > 6 h.
  Cron it from a scheduled job (e.g. Unraid User Scripts) to notify: `docker exec vault python3 /app/committer/staleness.py || <notify>`.

## Finding: BM polling can miss the final write of a burst
In ~1 of 8 runs of the 20-way append test BM indexed an intermediate version and never noticed the last one. Fix in the shim: after
`SETTLE_S` (6 s) of quiet on a path it bumps the file's mtime (content untouched), so the next poll re-checks it. With it: 24/24 integration runs
passed (several rescued at ~7.4 s). If a note is ever stale anyway: `basic-memory reindex --full` in the container (`docker exec vault ...`).

## Process model
`container/supervisor.py` is PID 1. All three children are critical: if any exits (even code 0) the others are stopped and the container
exits non-zero, so `restart: unless-stopped` restarts it (no half-alive states: shim up with BM dead, or committer dead = no backups). A drifted
config makes Basic Memory's entrypoint exit 78 after `DRIFT_EXIT_DELAY` (30 s throttle), which restarts the container in a loud, slow loop until fixed.
HEALTHCHECK (`healthcheck.py`): shim `/healthz`, BM port on loopback, committer heartbeat < 15 min.

## Unraid-only behaviour NOT verifiable on the sandbox
* **inotify on the FUSE share**: events for SMB/other-process writes on `/mnt/user` are unreliable. Compensation: BM runs with
  `WATCHFILES_FORCE_POLLING=1` (poll every 3 s; the integration test ran with polling and saw shim writes indexed in ~1.5 s). If a
  note ever fails to appear: `docker exec vault basic-memory reindex` (or `--full`). Optional belt-and-braces: User Script every
  30 min running that command.
* Committer sees file changes via `git status` (stat-based), so it is unaffected by inotify. SMB can leave 0-byte/half-written files
  while a big copy runs; the 15 s debounce covers normal saves, not a 10-minute copy.
* Ownership/permissions (99:100, umask 000) and the `docker compose` plugin on Unraid. SMB editors that save by rename-over (Obsidian.app)
  replace the file inode: fine for BM (polls), last-writer-wins vs the shim (no cross-process lock with SMB).

## Importing an existing vault
1. Stop anything that writes to the old store.
2. Copy the markdown (and attachments you want) into the vault folder, e.g. `rsync -a --exclude .obsidian /old/vault/ /mnt/user/data/Documents/Vault/`.
   Merge duplicates by hand with `diff -rq`; recent hand edits win.
3. Any file over 2 MB will not be indexed (listed in `.bmignore` at each start): `find /mnt/user/data/Documents/Vault -size +2M`, split or roll it.
4. Optional history: `git clone <your-existing-backup-remote> /mnt/user/data/Documents/Vault` first, then copy files over it; the committer continues that history.
5. Start the stack and run the checks above; `git status` must be clean (proves the safe config rewrote nothing).
6. If you open the folder in Obsidian.app over SMB, do not enable plugins that rewrite files.

## Troubleshooting
* **Container restarts with "REFUSING TO START: ..."** (token missing/empty/a directory/not readable by uid 99, or the state/vault folder not writable; the log line gives the exact `chown` fix; `./scripts/prepare.sh` does all of it; a directory at `secrets/vault_token` means it was missing when compose first ran): the token file did not exist
  when compose first ran, so Docker created a *directory* there. Fix: `rm -rf secrets/vault_token && ./scripts/prepare.sh && docker compose up -d`. Always create the token **before** the first `docker compose up`.
* **Committer logs "WARNING: backup remote ... unusable"**: the `/backup` mount is not writable by uid 99. The stack keeps committing locally but does not push.
  Fix: `mkdir -p <BACKUP_DIR> && chown 99:100 <BACKUP_DIR>` (default `/mnt/user/appdata/basic-memory/backup`), then restart. The log line carries git's real stderr.
* **`/search/simple/` returns 502 "BM unreachable"**: the message now includes the real cause. Loopback calls ignore `HTTP_PROXY`/`ALL_PROXY`
  (and the image sets `NO_PROXY=127.0.0.1,localhost`); the healthcheck does a real Basic Memory round trip, so a broken BM shows as `unhealthy`.

## Tests
`tests/run_all.sh` (stdlib only). Set `BM_BIN` to a `basic-memory==0.23.2` binary (Linux, py3.12 venv) to include the real-BM integration and the
supervised-stack checks. `VAULT_IP` is optional for tests (placeholder default). The compose file was only YAML-parsed and reviewed (no docker
available where this was built): the first `docker compose up` is the first container-level test.

## Upgrading Basic Memory
Pinned on purpose. To bump: change the version in `Dockerfile`, rebuild a throwaway on a synthetic vault, run `tests/integration_bm.py`
(the sha check proves it rewrites nothing) and re-check the settings in `bm/drift_guard.py` against the new default `config.json`.

## License
MIT, see `LICENSE`.
