import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BM_DIR = os.environ.get("BM_SRC") or os.path.join(ROOT, "bm")
GOOD = json.load(open(os.path.join(ROOT, "config", "config.json")))
GOOD["projects"]["main"]["path"] = "/vault"


class DriftBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfgdir = os.path.join(self.tmp.name, "cfg")
        os.makedirs(self.cfgdir)
        self.marker = os.path.join(self.tmp.name, "bm-started")
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "basic-memory")
        with open(stub, "w") as fh:  # stand-in for the real server: records that it started
            fh.write(f"#!/bin/sh\necho started > {self.marker}\nsleep 1\n")
        os.chmod(stub, 0o755)
        import shutil  # mimic the image layout: scripts + config.json in one STACK_DIR
        self.stack = os.path.join(self.tmp.name, "stack")
        shutil.copytree(BM_DIR, self.stack)
        shutil.copy(os.path.join(ROOT, "config", "config.json"), self.stack)
        self.env = {**os.environ, "PATH": bindir + os.pathsep + os.environ["PATH"],
                    "BASIC_MEMORY_CONFIG_DIR": self.cfgdir, "STACK_DIR": self.stack,
                    "VAULT_MOUNT": os.path.join(self.tmp.name, "vault"), "DRIFT_EXIT_DELAY": "0",
                    "DRIFT_RECHECK_S": "1"}
        os.makedirs(self.env["VAULT_MOUNT"])
        self.cfgfile = os.path.join(self.cfgdir, "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, cfg):
        with open(self.cfgfile, "w") as fh:
            json.dump(cfg, fh)

    def guard(self):
        return subprocess.run([sys.executable, os.path.join(BM_DIR, "drift_guard.py"), self.cfgfile],
                              capture_output=True, text=True, env=self.env)

    def entry(self):
        return subprocess.run(["sh", os.path.join(self.stack, "entrypoint.sh")], capture_output=True, text=True,
                              env=self.env, timeout=60)


class TestGuard(DriftBase):
    def setUp(self):
        super().setUp()
        self.env["VAULT_MOUNT"] = "/vault"

    def test_good_config_passes(self):
        self.put(GOOD)
        self.assertEqual(self.guard().returncode, 0)

    def test_each_deviation_refused(self):
        cases = {"ensure_frontmatter_on_sync": True, "disable_permalinks": False, "semantic_search_enabled": True,
                 "format_on_save": True, "update_permalinks_on_move": True, "kebab_filenames": True,
                 "auto_update": True}
        for key, bad in cases.items():
            cfg = dict(GOOD, **{key: bad})
            self.put(cfg)
            r = self.guard()
            self.assertEqual(r.returncode, 78, key)
            self.assertIn(key, r.stderr)
            self.assertIn("REFUSING TO START", r.stderr)

    def test_missing_key_and_string_false_refused(self):
        cfg = dict(GOOD)
        del cfg["ensure_frontmatter_on_sync"]
        self.put(cfg)
        self.assertEqual(self.guard().returncode, 78)
        self.put(dict(GOOD, disable_permalinks="true"))
        self.assertEqual(self.guard().returncode, 78)

    def test_project_path_and_extra_project_refused(self):
        bad = json.loads(json.dumps(GOOD))
        bad["projects"]["main"]["path"] = "/somewhere/else"
        self.put(bad)
        self.assertEqual(self.guard().returncode, 78)
        bad = json.loads(json.dumps(GOOD))
        bad["projects"]["extra"] = {"path": "/x"}
        self.put(bad)
        self.assertEqual(self.guard().returncode, 78)

    def test_unreadable_config_refused(self):
        with open(self.cfgfile, "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.guard().returncode, 66)


class TestEntrypoint(DriftBase):
    def good_cfg_for_env(self):
        cfg = json.loads(json.dumps(GOOD))
        cfg["projects"]["main"]["path"] = self.env["VAULT_MOUNT"]
        self.put(cfg)
        return cfg

    def test_drifted_config_refuses_to_start_bm(self):
        cfg = self.good_cfg_for_env()
        cfg["ensure_frontmatter_on_sync"] = True
        self.put(cfg)
        r = self.entry()
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertIn("REFUSING TO START", r.stderr)
        self.assertFalse(os.path.exists(self.marker), "Basic Memory must NOT have started")

    def test_good_config_starts_bm(self):
        self.good_cfg_for_env()
        r = self.entry()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.marker))

    def test_first_boot_seeds_safe_config_with_vault_path(self):
        # seeded copy has project path /vault; with VAULT_MOUNT=/vault the guard accepts it
        env = dict(self.env, VAULT_MOUNT="/vault")
        self.env = env
        r = self.entry()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.cfgfile))

    def test_runtime_drift_stops_bm_and_exits_nonzero(self):
        cfg = self.good_cfg_for_env()
        # long-running stub so the recheck loop has time to act
        stub = os.path.join(self.tmp.name, "bin", "basic-memory")
        with open(stub, "w") as fh:
            fh.write(f"#!/bin/sh\necho started > {self.marker}\nexec sleep 30\n")
        p = subprocess.Popen(["sh", os.path.join(self.stack, "entrypoint.sh")], env=self.env,
                             stderr=subprocess.PIPE, text=True)
        import time
        for _ in range(50):
            if os.path.exists(self.marker):
                break
            time.sleep(0.1)
        cfg["disable_permalinks"] = False
        self.put(cfg)
        err = p.communicate(timeout=20)[1]
        self.assertEqual(p.returncode, 78, err)
        self.assertIn("drifted at RUNTIME", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
