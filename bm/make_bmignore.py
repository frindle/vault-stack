#!/usr/bin/env python3
"""Maintain a managed block in $BASIC_MEMORY_CONFIG_DIR/.bmignore listing vault files
larger than MAX_INDEX_BYTES (default 2 MiB).

Basic Memory stalls and locks on notes above ~5 MB (trial 2026-10-01). .bmignore is
gitignore-style by NAME only (BM has no size-based ignore), so we regenerate the list
at every container start. The shim's append guard stops files growing past the cap in
the first place; this covers files that arrive some other way (SMB paste, migration).
Ignored files stay in git and on disk; they are just not indexed/searchable.
"""
import os
import sys

BEGIN = "# >>> vault-stack managed: oversize files (do not edit) >>>"
END = "# <<< vault-stack managed <<<"
DEFAULT_IGNORE = """# Basic Memory Ignore Patterns (seeded by vault-stack)
.*
*.db
*.db-shm
*.db-wal
config.json
.git
.obsidian
.DS_Store
Thumbs.db
desktop.ini
*.tmp
*.swp
*.swo
*~
"""


def find_oversize(root: str, limit: int) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                if os.path.isfile(p) and not os.path.islink(p) and os.path.getsize(p) > limit:
                    out.append(os.path.relpath(p, root).replace(os.sep, "/"))
            except OSError:
                pass
    return sorted(out)


def render(existing: str, oversize: list) -> str:
    lines = existing.splitlines()
    if BEGIN in lines and END in lines:
        i, j = lines.index(BEGIN), lines.index(END)
        lines = lines[:i] + lines[j + 1:]
    base = "\n".join(lines).rstrip("\n")
    block = [BEGIN] + ["/" + p for p in oversize] + [END]
    return (base + "\n\n" if base else "") + "\n".join(block) + "\n"


def main(argv) -> int:
    root = os.environ.get("VAULT_MOUNT", "/vault")
    cfg = os.environ.get("BASIC_MEMORY_CONFIG_DIR", "/config")
    limit = int(os.environ.get("MAX_INDEX_BYTES", str(2 * 1024 * 1024)))
    path = os.path.join(cfg, ".bmignore")
    try:
        existing = open(path, encoding="utf-8").read()
    except OSError:
        existing = DEFAULT_IGNORE
    over = find_oversize(root, limit)
    new = render(existing, over)
    if new != existing or not os.path.exists(path):
        os.makedirs(cfg, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
    for p in over:
        print(f"bmignore: NOT INDEXING oversize file ({limit} byte cap): {p}", file=sys.stderr)
    print(f"bmignore: {len(over)} oversize file(s) ignored", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
