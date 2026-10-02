#!/bin/sh
# Basic Memory entrypoint: seed config on first boot, DRIFT GUARD, size-ignore, run BM.
set -u
: "${BASIC_MEMORY_CONFIG_DIR:=/config}"
: "${DRIFT_EXIT_DELAY:=30}"          # throttle restart loops when refusing
: "${DRIFT_RECHECK_S:=60}"
: "${BM_PORT:=8000}"
export BASIC_MEMORY_CONFIG_DIR
STACK_DIR="${STACK_DIR:-/opt/vault-stack}"
GUARD="$STACK_DIR/drift_guard.py"
CFG="$BASIC_MEMORY_CONFIG_DIR/config.json"

mkdir -p "$BASIC_MEMORY_CONFIG_DIR" || exit 1
if [ ! -f "$CFG" ]; then
  echo "entrypoint: first boot, seeding $CFG from $STACK_DIR/config.json" >&2
  cp "$STACK_DIR/config.json" "$CFG" || exit 1
fi

refuse() {
  rc=$1
  echo "entrypoint: REFUSING TO START (guard exit $rc); sleeping ${DRIFT_EXIT_DELAY}s to throttle restarts" >&2
  sleep "$DRIFT_EXIT_DELAY"
  exit "$rc"
}

python3 "$GUARD" "$CFG" || refuse $?
python3 "$STACK_DIR/make_bmignore.py" || echo "entrypoint: WARN make_bmignore failed" >&2

basic-memory mcp --transport streamable-http --host "${BM_HOST:-127.0.0.1}" --port "$BM_PORT" --path /mcp &
BM_PID=$!
trap 'kill $BM_PID 2>/dev/null' TERM INT

# Runtime recheck: BM itself rewrites config.json (and so can a human).
(
  while sleep "$DRIFT_RECHECK_S"; do
    python3 "$GUARD" "$CFG" 2>/tmp/guard.err || {
      cat /tmp/guard.err >&2
      echo "entrypoint: config drifted at RUNTIME, stopping Basic Memory" >&2
      kill $BM_PID 2>/dev/null
      exit 0
    }
  done
) &
CHK_PID=$!

wait $BM_PID
rc=$?
kill $CHK_PID 2>/dev/null
# If we were killed by the runtime recheck, make the exit loud and nonzero.
python3 "$GUARD" "$CFG" 2>/dev/null || refuse 78
exit $rc
