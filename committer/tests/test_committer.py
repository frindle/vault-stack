import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("COMMITTER_SRC") or os.path.join(HERE, ".."))
import committer as cm  # noqa: E402
import staleness as st  # noqa: E402


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.repo, self.state, self.remote = f"{t}/vault", f"{t}/state", f"{t}/backup/vault.git"
        os.makedirs(self.repo)
        self.env = {"VAULT_ROOT": self.repo, "STATE_DIR": self.state, "COMMIT_DEBOUNCE_S": "5",
                    "LOCK_WAIT_S": "3", "STALE_LOCK_S": "100"}
        self.cfg = cm.Cfg(self.env)
        cm.ensure_repo(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text="x\n", age=60):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(text)
        os.utime(p, (time.time() - age, time.time() - age))
        return p

    def log_lines(self):
        return sh("git", "-C", self.repo, "log", "--format=%s").stdout.splitlines()


class TestCommitter(Base):
    def test_clean_does_nothing(self):
        self.assertEqual(cm.commit_if_dirty(self.cfg), "clean")
        self.assertEqual(self.log_lines(), [])

    def test_debounce_then_commit(self):
        self.write("Claude/a.md", age=0)
        self.assertEqual(cm.commit_if_dirty(self.cfg), "debounce")
        self.assertEqual(self.log_lines(), [])
        self.write("Claude/a.md", age=30)
        self.assertEqual(cm.commit_if_dirty(self.cfg), "committed")
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^vault: auto \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(cm.commit_if_dirty(self.cfg), "clean")

    def test_filemode_noise_ignored(self):
        p = self.write("a.md")
        cm.commit_if_dirty(self.cfg)
        os.chmod(p, 0o777)
        self.assertEqual(cm.commit_if_dirty(self.cfg), "clean")
        self.assertEqual(sh("git", "-C", self.repo, "config", "core.filemode").stdout.strip(), "false")

    def test_temp_files_excluded_and_deletes_committed(self):
        self.write("a.md")
        self.write(".a.md.abc123.tmp")
        cm.commit_if_dirty(self.cfg)
        self.assertEqual(sh("git", "-C", self.repo, "ls-files").stdout.split(), ["a.md"])
        os.unlink(os.path.join(self.repo, "a.md"))
        os.utime(self.repo, (time.time() - 60, time.time() - 60))  # dir mtime = when the delete happened
        self.assertEqual(cm.commit_if_dirty(self.cfg), "committed")
        self.assertEqual(sh("git", "-C", self.repo, "ls-files").stdout.split(), [])

    def test_live_index_lock_waited_out(self):
        self.write("a.md")
        lock = os.path.join(self.repo, ".git", "index.lock")
        open(lock, "w").close()
        threading.Timer(1.0, lambda: os.unlink(lock)).start()
        self.assertEqual(cm.commit_if_dirty(self.cfg), "committed")

    def test_stale_index_lock_removed(self):
        self.write("a.md")
        lock = os.path.join(self.repo, ".git", "index.lock")
        open(lock, "w").close()
        os.utime(lock, (time.time() - 1000, time.time() - 1000))
        self.assertEqual(cm.commit_if_dirty(self.cfg), "committed")
        self.assertFalse(os.path.exists(lock))

    def test_fresh_index_lock_persisting_is_not_removed(self):
        self.write("a.md")
        lock = os.path.join(self.repo, ".git", "index.lock")
        open(lock, "w").close()
        self.assertEqual(cm.commit_if_dirty(self.cfg), "locked")
        self.assertTrue(os.path.exists(lock))
        self.assertEqual(self.log_lines(), [])


class TestPush(Base):
    def setUp(self):
        super().setUp()
        self.env["COMMITTER_REMOTE"] = self.remote
        self.cfg = cm.Cfg(self.env)
        cm.ensure_repo(self.cfg)

    def test_push_created_bare_remote_and_idempotent(self):
        self.assertTrue(os.path.isdir(self.remote))
        self.assertEqual(cm.push_if_needed(self.cfg), "no-commits")
        self.write("a.md")
        cm.commit_if_dirty(self.cfg)
        self.assertEqual(cm.push_if_needed(self.cfg), "pushed")
        self.assertEqual(cm.push_if_needed(self.cfg), "up-to-date")
        self.assertEqual(sh("git", "-C", self.remote, "rev-parse", "main").stdout.strip(), cm.head_sha(self.cfg))
        self.write("b.md")
        cm.commit_if_dirty(self.cfg)
        self.assertEqual(cm.push_if_needed(self.cfg), "pushed")

    def test_unusable_backup_degrades_to_local_commits(self):
        blocker = os.path.join(self.tmp.name, "afile")
        open(blocker, "w").write("x")
        self.env["COMMITTER_REMOTE"] = os.path.join(blocker, "vault.git")  # parent is a file: cannot be created
        cfg = cm.Cfg(self.env)
        os.environ["COMMITTER_REMOTE"] = self.env["COMMITTER_REMOTE"]
        try:
            cm.ensure_repo(cfg)  # must NOT raise
            self.assertEqual(cfg.remote, "")
            self.assertEqual(os.environ["COMMITTER_REMOTE"], "")
        finally:
            os.environ.pop("COMMITTER_REMOTE", None)
        self.write("a.md")
        self.assertEqual(cm.commit_if_dirty(cfg), "committed")
        self.assertEqual(cm.push_if_needed(cfg), "no-remote")

    def test_failed_push_retried(self):
        self.write("a.md")
        cm.commit_if_dirty(self.cfg)
        os.rename(self.remote, self.remote + ".gone")
        self.assertEqual(cm.push_if_needed(self.cfg), "failed")
        os.rename(self.remote + ".gone", self.remote)
        self.assertEqual(cm.push_if_needed(self.cfg), "pushed")

    def test_cycle_writes_heartbeat_and_pushes(self):
        self.write("a.md")
        os.environ.update({"VAULT_ROOT": self.repo, "STATE_DIR": self.state, "COMMITTER_REMOTE": self.remote})
        try:
            self.assertEqual(cm.cycle(self.cfg), ("committed", "pushed"))
        finally:
            for k in ("VAULT_ROOT", "STATE_DIR", "COMMITTER_REMOTE"):
                os.environ.pop(k, None)
        self.assertTrue(os.path.exists(f"{self.state}/committer.heartbeat"))


class TestStaleness(Base):
    def scfg(self, **kw):
        e = dict(self.env, **kw)
        return st.Cfg(e)

    def beat(self, when):
        os.makedirs(self.state, exist_ok=True)
        with open(f"{self.state}/committer.heartbeat", "w") as fh:
            fh.write(str(int(when)))

    def test_healthy(self):
        self.write("a.md")
        cm.commit_if_dirty(self.cfg)
        self.beat(time.time())
        self.assertEqual(st.run(self.scfg()), [])
        self.assertFalse(os.path.exists(f"{self.state}/STALE"))

    def test_head_old_and_dirty_alarms_and_writes_flag(self):
        self.write("a.md")
        cm.commit_if_dirty(self.cfg)
        self.write("b.md")  # dirty
        now = time.time() + 7 * 3600
        self.beat(now)
        reasons = st.run(self.scfg(), now=now)
        self.assertTrue(any("HEAD is" in r and "dirty" in r for r in reasons), reasons)
        flag = json.load(open(f"{self.state}/STALE"))
        self.assertEqual(flag["reasons"], reasons)
        # recovery: commit, flag cleared
        self.write("b.md", age=60)
        cm.commit_if_dirty(self.cfg)
        self.assertEqual(st.run(self.scfg(), now=time.time()), [])
        self.assertFalse(os.path.exists(f"{self.state}/STALE"))

    def test_old_head_but_clean_and_committer_alive_is_fine(self):
        self.write("a.md")
        cm.commit_if_dirty(self.cfg)
        now = time.time() + 30 * 3600
        self.beat(now)
        self.assertEqual(st.run(self.scfg(), now=now), [])

    def test_dead_committer_alarms(self):
        self.write("a.md")
        cm.commit_if_dirty(self.cfg)
        self.assertTrue(any("never run" in r for r in st.run(self.scfg())))
        self.beat(time.time() - 3600)
        self.assertTrue(any("not running" in r for r in st.run(self.scfg())))

    def test_unpushed_too_long_alarms(self):
        self.write("a.md")
        cm.commit_if_dirty(self.cfg)
        now = time.time() + 7 * 3600
        self.beat(now)
        reasons = st.run(self.scfg(COMMITTER_REMOTE=self.remote), now=now)
        self.assertTrue(any("never received HEAD" in r for r in reasons), reasons)

    def test_cli_exit_codes(self):
        self.write("a.md")
        cm.commit_if_dirty(self.cfg)
        env = {**os.environ, **self.env}
        script = os.path.join(os.path.dirname(st.__file__), "staleness.py")
        p = subprocess.run([sys.executable, script], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)  # no heartbeat yet
        self.assertIn("VAULT BACKUP STALE", p.stderr)
        self.beat(time.time())
        p = subprocess.run([sys.executable, script], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
