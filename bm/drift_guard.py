#!/usr/bin/env python3
"""Drift guard for Basic Memory's config.json.

Basic Memory's DEFAULT config rewrites ~90% of the vault's files on first sync
(adds frontmatter + permalinks). Trial finding 2026-10-01: only the settings in
REQUIRED keep the vault byte-identical. If config.json deviates (an upgrade, a
`basic-memory config set`, someone editing it) we refuse to run BM at all.

Usage: drift_guard.py [path/to/config.json]      (default $BASIC_MEMORY_CONFIG_DIR/config.json)
Exit 0 = ok.  Exit 78 = drift (loud message on stderr).  Exit 66 = config missing/unreadable.
"""
import json
import os
import sys

REQUIRED = {
    "ensure_frontmatter_on_sync": False,
    "disable_permalinks": True,
    "semantic_search_enabled": False,
    "format_on_save": False,
    "update_permalinks_on_move": False,
    "kebab_filenames": False,
    "auto_update": False,
}
# The single project must point at the vault mount; anything else means BM would
# index (and "fix up") some other folder.
EXPECTED_PROJECT = ("main", os.environ.get("VAULT_MOUNT", "/vault"))

EXIT_DRIFT = 78
EXIT_MISSING = 66


def check(cfg: dict) -> list:
    problems = []
    for key, want in REQUIRED.items():
        if key not in cfg:
            problems.append(f"{key}: MISSING (required {want!r})")
        elif cfg[key] is not want:  # `is` so 0/1 or "false" strings do not pass
            problems.append(f"{key}: is {cfg[key]!r}, required {want!r}")
    name, path = EXPECTED_PROJECT
    projects = cfg.get("projects")
    proj = projects.get(name) if isinstance(projects, dict) else None
    if not isinstance(proj, dict) or os.path.normpath(str(proj.get("path", ""))) != os.path.normpath(path):
        problems.append(f"projects.{name}.path: is {proj!r}, required {path!r}")
    if len(projects or {}) != 1:
        problems.append(f"projects: expected exactly one project, found {sorted(projects or {})}")
    if cfg.get("default_project") != name:
        problems.append(f"default_project: is {cfg.get('default_project')!r}, required {name!r}")
    return problems


def main(argv) -> int:
    path = argv[1] if len(argv) > 1 else os.path.join(
        os.environ.get("BASIC_MEMORY_CONFIG_DIR", "/config"), "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"DRIFT GUARD: cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_MISSING
    problems = check(cfg)
    if problems:
        bar = "!" * 72
        print(bar, file=sys.stderr)
        print(f"DRIFT GUARD: REFUSING TO START Basic Memory. {path} deviates from the safe config.",
              file=sys.stderr)
        print("Running with these settings would REWRITE vault files (frontmatter/permalinks).", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("Fix: restore config/config.json from the repo (see README 'Drift guard'), then restart.",
              file=sys.stderr)
        print(bar, file=sys.stderr)
        return EXIT_DRIFT
    print("drift guard: config ok", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
