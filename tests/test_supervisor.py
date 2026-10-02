import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUP = os.path.join(os.environ.get("SUPERVISOR_SRC") or os.path.join(ROOT, "container"), "supervisor.py")


def sleeper(name, secs, marker=None):
    code = f"import time,sys\nopen({marker!r},'a').write('{name} up\\n')\ntime.sleep({secs})" if marker else f"import time\ntime.sleep({secs})"
    return {"name": name, "cmd": [sys.executable, "-c", code]}


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class TestSupervisor(unittest.TestCase):
    def run_sup(self, children, **env):
        p = subprocess.Popen([sys.executable, SUP], env={**os.environ, "SUPERVISOR_CHILDREN": json.dumps(children),
                                                            "SUPERVISOR_GRACE_S": "3", **env},
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def test_token_dir_or_missing_or_empty_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as d:
            empty = os.path.join(d, "empty")
            open(empty, "w").write("\n")
            good = os.path.join(d, "good")
            open(good, "w").write("tok\n")
            for path, label in ((d, "DIRECTORY"), (os.path.join(d, "nope"), "missing"), (empty, "empty")):
                p = self.run_sup([sleeper("a", 5)], VAULT_TOKEN_FILE=path, TOKEN_EXIT_DELAY="0")
                err = p.communicate(timeout=20)[1]
                self.assertEqual(p.returncode, 78, (label, err))
                self.assertIn(label, err)
                self.assertIn("openssl rand -hex 32 > secrets/vault_token", err)
                self.assertNotIn("started a", err)
            p = self.run_sup([sleeper("a", 1)], VAULT_TOKEN_FILE=good, TOKEN_EXIT_DELAY="0")
            self.assertIn("started a", p.communicate(timeout=20)[1])

    def _load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sup_under_test", SUP)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_unreadable_token_and_unwritable_dirs_give_exact_fix(self):
        from unittest import mock
        m = self._load()
        with tempfile.TemporaryDirectory() as d:
            tok = os.path.join(d, "tok")
            open(tok, "w").write("secret\n")
            env = {"VAULT_TOKEN_FILE": tok, "BASIC_MEMORY_CONFIG_DIR": d, "VAULT_ROOT": d,
                   "COMMITTER_REMOTE": os.path.join(d, "backup", "vault.git")}
            os.makedirs(os.path.join(d, "backup"))
            self.assertEqual(m.preflight(env), [])
            real = os.access
            # simulate "owned by root, 0600/0755, container runs as another uid"
            with mock.patch.object(m.os, "access", lambda p, mode: False if str(p).startswith(d) else real(p, mode)), \
                 mock.patch.object(m.os, "getuid", lambda: 99), mock.patch.object(m.os, "getgid", lambda: 100):
                got = m.preflight(env)
            text = " | ".join(f"{w} || {f}" for w, f, _ in got)
            self.assertIn("NOT READABLE by uid 99:100", text)
            self.assertIn("chown 99:100 secrets/vault_token && chmod 400 secrets/vault_token", text)
            self.assertIn("chown -R 99:100 <APPDATA_DIR>", text)
            self.assertIn("chown -R 99:100 <VAULT_DIR>", text)
            self.assertIn("chown -R 99:100 <BACKUP_DIR>", text)
            fatal = {w.split()[0] for w, _, f in got if f}
            self.assertNotIn(os.path.join(d, "backup"), fatal)
            self.assertEqual([f for w, _, f in got if "backup dir" in w], [False])  # backup is a warning only

    def test_unreadable_token_refuses_to_start(self):
        # end to end: a token the supervisor cannot read -> exit 78 before any child starts
        if os.getuid() == 0:
            self.skipTest("root bypasses file permissions; covered by the mocked test")
        with tempfile.TemporaryDirectory() as d:
            tok = os.path.join(d, "tok")
            open(tok, "w").write("secret\n")
            os.chmod(tok, 0)
            p = self.run_sup([sleeper("a", 5)], VAULT_TOKEN_FILE=tok, TOKEN_EXIT_DELAY="0")
            err = p.communicate(timeout=20)[1]
            self.assertEqual(p.returncode, 78, err)
            self.assertIn("NOT READABLE", err)

    def test_any_child_death_exits_nonzero_and_kills_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            pidf = os.path.join(d, "pid")
            long_lived = {"name": "long", "cmd": [sys.executable, "-c",
                          f"import os,time;open({pidf!r},'w').write(str(os.getpid()));time.sleep(8)"]}
            dies = {"name": "dies", "cmd": [sys.executable, "-c", "import time,sys;time.sleep(1);sys.exit(0)"]}
            p = self.run_sup([long_lived, dies])
            err = p.communicate(timeout=20)[1]
            self.assertNotEqual(p.returncode, 0, "exit 0 child must still be fatal")
            self.assertIn("CRITICAL child dies", err)
            time.sleep(0.5)
            self.assertFalse(alive(int(open(pidf).read())), "surviving child must be terminated")

    def test_child_exit_code_propagates(self):
        p = self.run_sup([sleeper("a", 8), {"name": "b", "cmd": [sys.executable, "-c", "import sys;sys.exit(78)"]}])
        p.communicate(timeout=20)
        self.assertEqual(p.returncode, 78)

    def test_sigterm_stops_children_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            pidf = os.path.join(d, "pid")
            c = {"name": "long", "cmd": [sys.executable, "-c",
                 f"import os,time;open({pidf!r},'w').write(str(os.getpid()));time.sleep(8)"]}
            p = self.run_sup([c, sleeper("x", 8)])
            for _ in range(50):
                if os.path.exists(pidf) and open(pidf).read():
                    break
                time.sleep(0.1)
            time.sleep(0.3)
            p.send_signal(signal.SIGTERM)
            p.communicate(timeout=20)
            self.assertEqual(p.returncode, 0)
            time.sleep(0.3)
            self.assertFalse(alive(int(open(pidf).read())))

    def test_healthcheck_reports_dead_components(self):
        hc = os.path.join(ROOT, "container", "healthcheck.py")
        env = {**os.environ, "SHIM_PORT": "1", "BM_PORT": "1", "STATE_DIR": "/nonexistent", "HC_START": "1"}
        r = subprocess.run([sys.executable, hc], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("shim", r.stderr)
        self.assertIn("basic-memory", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
