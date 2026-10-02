#!/bin/sh
# Unit + revert tests always; integration + supervised-stack tests when BM_BIN is set.
cd "$(dirname "$0")/.." || exit 1
rc=0
run() { out=$("$@" 2>&1); r=$?; printf '%s\n' "$out" | grep -E "^(Ran |OK|FAILED|\[(PASS|FAIL|BITES)\]|[0-9]+/[0-9]+ |ALL GUARDS|SOME GUARDS)"; [ $r -eq 0 ] || rc=1; }
run python3 -m unittest discover -s shim/tests
run python3 -m unittest discover -s committer/tests
run python3 -m unittest discover -s tests -p "test_drift*.py"
run python3 -m unittest discover -s tests -p "test_supervisor.py"
run python3 tests/revert_tests.py
[ -n "${BM_BIN:-}" ] && run python3 tests/integration_bm.py
[ -n "${BM_BIN:-}" ] && run python3 tests/supervised_check.py
echo "run_all exit=$rc"
exit $rc
