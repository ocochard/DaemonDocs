#!/bin/sh
# run_all.sh — run every test suite, report a summary, exit non-zero on failure.
#
# Usage:  tests/run_all.sh            (uses ../.venv/bin/python if present)
#         PYTHON=python3 tests/run_all.sh
#
# Each test is a standalone script that imports generate-doc.py via
# importlib (the hyphen in the filename blocks a normal import) and exits
# non-zero on failure. No pytest dependency.

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")

if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -x "$REPO/.venv/bin/python" ]; then
    PY="$REPO/.venv/bin/python"
else
    PY=python3
fi

echo "python: $PY"
echo "repo:   $REPO"
echo

pass=0
fail=0
failed=""

for t in "$HERE"/test_*.py; do
    name=$(basename "$t")
    printf '%-40s ' "$name"
    if out=$("$PY" "$t" 2>&1); then
        echo "OK"
        pass=$((pass + 1))
    else
        echo "FAIL"
        echo "$out" | tail -15 | sed 's/^/    /'
        fail=$((fail + 1))
        failed="$failed $name"
    fi
done

echo
echo "passed: $pass   failed: $fail"
if [ "$fail" -gt 0 ]; then
    echo "failing:$failed"
    exit 1
fi
