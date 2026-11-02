#!/usr/bin/env bash
# Smoke test of a PACKAGED (or any) sidecar: start it, then exercise it over HTTP.
#
#   scripts/smoke_sidecar.sh <expected_version> <samples_dir> <sidecar command...>
#
# The sidecar command gets the port as its last argument (as Electron does) and the token in
# METALENS_SIDECAR_TOKEN. <samples_dir> holds files made by `python -m tests.factories --out`.
# Checks: /health (status + version), auth is enforced, /list, /read of every sample with no
# read errors (this loads every handler and library bundled by PyInstaller) and a /write + /undo
# round trip on a PDF. Needs only bash + curl, so it runs inside minimal containers.
set -u

if [ "$#" -lt 3 ]; then
  echo "usage: $0 <expected_version> <samples_dir> <sidecar command...>" >&2
  exit 2
fi
EXPECTED="$1"; SAMPLES="$2"; shift 2

PORT=$((20000 + RANDOM % 20000))
TOKEN=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
LOG=$(mktemp)
FAILS=0

fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
ok()   { echo "ok:   $*"; }

# Paths are sent to the sidecar as it understands them (native Windows paths under Git Bash).
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
BS=$'\134'   # a single backslash
json_escape() { local s=${1//"$BS"/"$BS$BS"}; printf '%s' "${s//\"/$BS\"}"; }
api() { curl -s --max-time 30 -H "Authorization: Bearer $TOKEN" "$@"; }

METALENS_SIDECAR_TOKEN="$TOKEN" "$@" "$PORT" >"$LOG" 2>&1 &
PID=$!
cleanup() {
  kill "$PID" >/dev/null 2>&1
  # A PyInstaller --onefile sidecar is a bootstrap plus a child that serves: stop both.
  # -x: match the process NAME only; -f would also match this script (its path contains the name).
  pkill -x "metalens-sidecar" >/dev/null 2>&1
  command -v taskkill >/dev/null 2>&1 && taskkill //F //T //PID "$PID" >/dev/null 2>&1
  rm -f "$LOG"
}
trap cleanup EXIT

# ── /health ──────────────────────────────────────────────────────────────────────────────
HEALTH=""
for _ in $(seq 1 60); do
  HEALTH=$(api "http://127.0.0.1:$PORT/health" 2>/dev/null)
  case "$HEALTH" in *'"status":"ok"'*) break ;; esac
  kill -0 "$PID" >/dev/null 2>&1 || { echo "sidecar exited early:"; cat "$LOG"; exit 1; }
  sleep 1
done
case "$HEALTH" in
  *'"status":"ok"'*) ok "health: $HEALTH" ;;
  *) echo "sidecar never became healthy:"; cat "$LOG"; exit 1 ;;
esac
case "$HEALTH" in
  *"\"version\":\"$EXPECTED\""*) ok "version is $EXPECTED" ;;
  *) fail "expected version $EXPECTED, got: $HEALTH" ;;
esac

# ── auth is enforced ─────────────────────────────────────────────────────────────────────
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://127.0.0.1:$PORT/health")
[ "$CODE" = "401" ] && ok "request without token -> 401" || fail "request without token -> $CODE (expected 401)"

# ── /list ────────────────────────────────────────────────────────────────────────────────
CODE=$(api -o /dev/null -w '%{http_code}' --get --data-urlencode "path=$(native "$SAMPLES")" "http://127.0.0.1:$PORT/list")
[ "$CODE" = "200" ] && ok "/list of the samples folder -> 200" || fail "/list -> $CODE"

# ── /read of every sample ────────────────────────────────────────────────────────────────
COUNT=0
while IFS= read -r FILE; do
  COUNT=$((COUNT + 1))
  BODY=$(api -w '\n%{http_code}' --get --data-urlencode "path=$(native "$FILE")" "http://127.0.0.1:$PORT/read")
  CODE=${BODY##*$'\n'}; BODY=${BODY%$'\n'*}
  case "$CODE:$BODY" in
    200:*'"read_errors":[]'*) ok "read ${FILE#"$SAMPLES"/}" ;;
    200:*) fail "read ${FILE#"$SAMPLES"/}: read_errors not empty" ;;
    *) fail "read ${FILE#"$SAMPLES"/}: HTTP $CODE ${BODY:0:200}" ;;
  esac
done < <(find "$SAMPLES" -type f | sort)
[ "$COUNT" -gt 0 ] || fail "no sample files found in $SAMPLES"

# ── /write + /undo round trip on a PDF ───────────────────────────────────────────────────
PDF=$(find "$SAMPLES" -type f -name '*.pdf' | sort | head -1)
if [ -n "$PDF" ]; then
  P=$(json_escape "$(native "$PDF")")
  W=$(api -X POST -H 'Content-Type: application/json' \
        -d "{\"path\":\"$P\",\"fields\":[{\"key\":\"pdf:Title\",\"value\":\"MetaLens smoke test\"}],\"delete\":[]}" \
        "http://127.0.0.1:$PORT/write")
  case "$W" in
    *'"ok":true'*'"undo_token":"'*)
      TK=${W#*\"undo_token\":\"}; TK=${TK%%\"*}
      ok "write pdf:Title"
      U=$(api -X POST -H 'Content-Type: application/json' -d "{\"token\":\"$TK\"}" "http://127.0.0.1:$PORT/undo")
      case "$U" in *'"ok":true'*) ok "undo restored the PDF" ;; *) fail "undo: ${U:0:200}" ;; esac ;;
    *) fail "write pdf:Title: ${W:0:300}" ;;
  esac
else
  fail "no PDF sample for the write/undo round trip"
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "SMOKE TEST PASSED ($COUNT files read)"; else echo "SMOKE TEST FAILED: $FAILS check(s)"; echo "--- sidecar log ---"; cat "$LOG"; fi
[ "$FAILS" -eq 0 ]
