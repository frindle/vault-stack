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
