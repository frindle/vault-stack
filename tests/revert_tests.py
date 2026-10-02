#!/usr/bin/env python3
"""Revert-tests: prove each guard BITES. For every mutation we copy the source tree,
break one guard, and require the matching test to FAIL (and pass on the pristine copy).
Exit 0 only if all pristine runs pass and all mutated runs fail.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NOLOCK = '''class _NoLock:
    def __call__(self, *keys):
        import contextlib
        return contextlib.nullcontext()
'''

MUTATIONS = [
    # (name, file, old, new, test-module-path, test selector, env var for the source dir)
    ("break per-path lock -> 20-way append test", "shim/vault_shim.py",
     "self.locks = LockTable()", "self.locks = _NoLock()", "shim/tests", "test_shim.TestConcurrency", "SHIM_SRC",
     NOLOCK),
    ("BM client uses proxy-honouring urlopen -> proxy-env test", "shim/vault_shim.py",
     "with _LOOPBACK_OPENER.open(req, timeout=self.timeout) as r:",
     "with urllib.request.urlopen(req, timeout=self.timeout) as r:", "shim/tests",
     "test_shim.TestBMProxy.test_search_ignores_proxy_env", "SHIM_SRC", ""),
    ("remove size guard -> size-guard tests", "shim/vault_shim.py",
     "    def _guard_size(self, rel, old_size, new_size):\n",
     "    def _guard_size(self, rel, old_size, new_size):\n        return\n", "shim/tests", "test_shim.TestSizeGuard",
     "SHIM_SRC", ""),
    ("remove MCP write-tool block -> edge-block tests", "shim/vault_shim.py",
     "            blocked = mcp_block_response(body, self.app.blocklist.names())\n",
     "            blocked = None\n", "shim/tests", "test_shim.TestMCPEdgeBlock", "SHIM_SRC", ""),
    ("remove settle touch -> settle test", "shim/vault_shim.py",
     "        if self.cfg.settle_s <= 0:\n            return\n", "        return\n", "shim/tests",
     "test_shim.TestSettleTouch", "SHIM_SRC", ""),
    ("supervisor ignores child death -> supervisor test", "container/supervisor.py",
     "        if dead:\n", "        if False:\n", "tests", "test_supervisor.TestSupervisor.test_any_child_death_exits_nonzero_and_kills_the_rest",
     "SUPERVISOR_SRC", ""),
    ("remove drift guard check -> guard tests", "bm/drift_guard.py",
     "    problems = check(cfg)\n", "    problems = []\n", "tests", "test_drift_guard.TestGuard", "BM_SRC", ""),
    ("remove drift guard call from entrypoint -> refuse-to-start test", "bm/entrypoint.sh",
     'python3 "$GUARD" "$CFG" || refuse $?\n', "", "tests",
     "test_drift_guard.TestEntrypoint.test_drifted_config_refuses_to_start_bm", "BM_SRC", ""),
    ("remove staleness alarm -> staleness tests", "committer/staleness.py",
     "    reasons = []\n    try:\n        hb =", "    reasons = []\n    return reasons\n    try:\n        hb =",
     "committer/tests", "test_committer.TestStaleness", "COMMITTER_SRC", ""),
    ("remove token precheck -> refuse-to-start test", "container/supervisor.py",
     "    if any(f for _, _, f in problems):\n", "    if False:\n", "tests",
     "test_supervisor.TestSupervisor.test_token_dir_or_missing_or_empty_refuses_to_start", "SUPERVISOR_SRC", ""),
    ("drop unreadable-token check -> exact-fix message test", "container/supervisor.py",
     "    if not os.access(path, os.R_OK):\n", "    if False:\n", "tests",
     "test_supervisor.TestSupervisor.test_unreadable_token_and_unwritable_dirs_give_exact_fix", "SUPERVISOR_SRC", ""),
    ("committer fails hard on unusable backup -> degrade test", "committer/committer.py",
     "        except Exception as e:  # an unusable backup", "        except ZeroDivisionError as e:  # an unusable backup",
     "committer/tests", "test_committer.TestPush.test_unusable_backup_degrades_to_local_commits", "COMMITTER_SRC", ""),
    ("remove committer debounce -> debounce test", "committer/committer.py",
     "    if now - newest_mtime(cfg, files) < cfg.debounce:\n        return \"debounce\"\n", "",
     "committer/tests", "test_committer.TestCommitter.test_debounce_then_commit", "COMMITTER_SRC", ""),
]


def run_tests(tests_dir, selector, env_name, src_dir):
    env = dict(os.environ)
    if src_dir:
        env[env_name] = src_dir
    p = subprocess.run([sys.executable, "-m", "unittest", selector], cwd=os.path.join(ROOT, tests_dir),
                       env=env, capture_output=True, text=True, timeout=600)
    return p.returncode, p.stderr[-600:]


def main():
    ok = True
    for name, rel, old, new, tdir, sel, envn, extra in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            src_sub = os.path.dirname(rel)
            dst = os.path.join(tmp, src_sub)
            shutil.copytree(os.path.join(ROOT, src_sub), dst, ignore=shutil.ignore_patterns("tests", "__pycache__"))
            f = os.path.join(tmp, rel)
            text = open(f).read()
            if old not in text:
                print(f"BROKEN MUTATION (pattern not found): {name}")
                ok = False
                continue
            mutated = text.replace(old, new, 1)
            if extra:
                mutated = mutated.replace("class LockTable:", extra + "\n\nclass LockTable:", 1)
            open(f, "w").write(mutated)
            if rel.startswith("committer/") and "staleness" not in rel:
                shutil.copy(os.path.join(ROOT, "committer/staleness.py"), dst)
            if rel == "committer/staleness.py":
                shutil.copy(os.path.join(ROOT, "committer/committer.py"), dst)
            good_rc, _ = run_tests(tdir, sel, envn, None)
            bad_rc, tail = run_tests(tdir, sel, envn, dst)
            verdict = "BITES" if (good_rc == 0 and bad_rc != 0) else "FAIL"
            if verdict != "BITES":
                ok = False
            print(f"[{verdict}] {name}: pristine rc={good_rc}, mutated rc={bad_rc}")
            if verdict != "BITES":
                print(tail)
    print("ALL GUARDS BITE" if ok else "SOME GUARDS DO NOT BITE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
