#!/bin/sh
# runner.sh — pull chapter numbers from queue.txt and regenerate them.
#
# Usage: runner.sh LABEL OPENAI_BASE_URL [REVIEWER_THINKING]
#   LABEL — short tag for the log files (e.g. fw, fw2)
#   OPENAI_BASE_URL — full URL of a llama-server, e.g. http://HOST:8080/v1
#     (public repo: the real LAN addresses are not recorded here)
#   REVIEWER_THINKING — 1 (default) or 0, controls the REVIEWER only.
#     Running the two endpoints with opposite values against the shared
#     queue is a live A/B: chapters land in one arm or the other by
#     whichever runner popped them.
#
# Writer reasoning is pinned ON below and must stay that way. Turning it
# off is ~3x faster per call and far more expensive per chapter: the
# writer replaces deliberation with tool calls and never converges (24
# repeat calls on one symbol, 2 max_steps hits, vs 0 with thinking on).
# It killed ch3/ch4/ch5/ch6 at max_steps=80 on the 2026-08-22 run.
#
# Pop is atomic via lockf(1) on queue.lock. Append to queue.txt is safe
# without locking (write-append is atomic for short lines on FreeBSD).
# Loop exits when queue.txt is empty (or only whitespace remains).
#
# Crash recovery: popping a chapter and finishing it are not one
# transaction, so a host reboot loses whatever was in flight — the number
# is already out of queue.txt and nothing records that it never finished.
# The 2026-08-28 power outage swallowed chapters 25 and 26 exactly this
# way: both had been popped, both died mid-draft, queue.txt resumed at 27,
# and a plain restart would have skipped them silently with no error in
# any log. To close that hole each runner writes its popped number to
# queue.inflight.$LABEL inside the same lockf critical section as the pop,
# and removes the file once the chapter finishes (success, failure, or
# watchdog kill — all three are "no longer in flight").
#
# Recovery is deliberately NOT automatic at startup. A runner cannot tell
# "I crashed with this chapter in flight" from "another copy of me is
# working on this chapter right now" — auto-reclaiming would double-
# generate whenever a second runner is started by mistake. So:
#   normal start  -> warns about a stale inflight file, leaves it alone
#   --recover     -> pushes stale inflight numbers back onto the front of
#                    the queue, then proceeds as usual
# Use --recover after a reboot or a hard kill, once you have confirmed no
# runner with that label is still alive (pgrep -f "runner.sh $LABEL").
#
# Watchdog: a sidecar shell loop polls the per-chapter log file's mtime
# every 60s. If the log hasn't grown in WATCHDOG_SECS (default 1200s =
# 20 min) while python is still running, SIGTERM python and let the
# next iteration pop the next chapter. Catches any kind of stall —
# runaway thinking, smolagents deadlock, network wedge — with one
# language-agnostic mechanism.

set -u

# --recover may appear anywhere; strip it out before reading positionals so
# the existing "runner.sh LABEL URL [RTHINK]" call sites keep working.
RECOVER=0
args=""
for a in "$@"; do
    case "$a" in
        --recover) RECOVER=1 ;;
        *) args="$args $a" ;;
    esac
done
# Word-splitting here is intentional: every arg is a label, URL or digit.
# shellcheck disable=SC2086
set -- $args

LABEL="${1:?LABEL required}"
URL="${2:?OPENAI_BASE_URL required}"
RTHINK="${3:-1}"
# Step 3 rationale rubric: OFF unless this arm explicitly opts in.
# Default must stay 0 to match generate-doc.py, so an arm launched
# without the 4th argument runs today's rubric unchanged.
RATENUM="${4:-0}"

# Directory holding queue.txt, logs and inflight state. Defaults to the
# directory this script lives in, so moving the harness needs no edit.
QUEUE_DIR="${REGEN_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# `./runner.sh` invoked from a different cwd resolves dirname to THAT cwd,
# which would silently run against the wrong directory. Require the dir to
# actually hold this script and its siblings, so a stray copy elsewhere
# cannot pass. Keyed off $0's own basename because this file is deployed
# under a different name (runner.sh) than it carries here.
SELF_NAME=$(basename "$0")
if [ ! -f "$QUEUE_DIR/$SELF_NAME" ] || [ ! -f "$QUEUE_DIR/ab-verdicts.awk" ]; then
    echo "$SELF_NAME: '$QUEUE_DIR' is not the harness dir; set REGEN_DIR" >&2
    exit 1
fi
# Single-instance interlock per label. Two runners sharing one label both
# pop from the same queue and fight over the same inflight file: the second
# one declares the first one's live chapter "stale", then pops and burns
# further chapters. That happened on 2026-09-02 (a stray `runner.sh fw x`
# set ch25 aside as stale and destroyed ch13 and ch21 against a bogus
# endpoint) and cost two chapters. The stale-inflight path below tells the
# operator to run this pgrep by hand; doing it here is what actually stops
# the collision.
#
# Matching must be anchored to "a shell RUNNING this script", not to the
# bare string, because the string appears in unrelated command lines:
#
#   - any wrapper/editor/agent shell whose argv or heredoc merely
#     CONTAINS `runner.sh fw ...` (an interactive `sh -c` launching this
#     script matches itself, so a legitimate first launch is refused);
#   - grep/tail/pgrep pipelines typed by the operator.
#
# daemon(8) is NOT one of them: it retitles itself to
# `daemon: <path>[<childpid>]`, so the supervisor never matches. That was
# misdiagnosed once (2026-09-02) and cost a wrong "exclude $PPID" patch.
#
# `pgrep -lf` (pid + argv; FreeBSD has no `-a`) plus an interpreter
# prefix keeps exactly the real case: `/bin/sh ./runner.sh fw <url>`.
# Every daemon(8) launch silently returned rc=0 while being refused,
# because daemon detaches before the child's stderr exists -- the
# refusal is visible only with `daemon -o`.
#
# $$ is excluded because pgrep matches this very process. Advisory-only
# against a determined racer (two simultaneous starts could both pass)
# but it catches every accidental double-launch, which is the real
# failure mode.
# Dots in the basename are escaped: unescaped, `runner.sh` would also
# match `runnerXsh`. Harmless here, but the deployed copy escaped it and
# a regex that quietly widens is not worth inheriting.
SELF_RE=$(echo "$SELF_NAME" | sed 's/[.[\*^$]/\\&/g')
OTHERS=$(pgrep -lf "$SELF_RE $LABEL( |$)" 2>/dev/null \
    | grep -E '^[0-9]+ (/[^ ]*/)?[a-z]*sh +[^ ]*'"$SELF_RE"' ' \
    | awk '{print $1}' | grep -v "^$$\$" | tr '\n' ' ')
if [ -n "$(echo "$OTHERS" | tr -d ' ')" ]; then
    echo "$SELF_NAME: a runner for label '$LABEL' is already alive (pid$OTHERS); refusing to start a second one. Kill it first, or use a different LABEL." >&2
    exit 1
fi

# Queue file is overridable so an A/B can give each arm its own chapter
# list; unset it and the shared queue.txt behaves exactly as before.
QUEUE="${QUEUE_FILE:-$QUEUE_DIR/queue.txt}"
LOCK="$QUEUE_DIR/queue.lock"
LOG="$QUEUE_DIR/$LABEL.log"
INFLIGHT="$QUEUE_DIR/queue.inflight.$LABEL"

# Max idle (no log growth) seconds before we kill python.
#
# Was 1200s, raised to 2400s on 2026-08-22. Measured against chapters that
# COMPLETED SUCCESSFULLY under Qwen3.8-27B: ch1's slowest legitimate step
# was 1164s and ch2's was 974s — ch1 came within 3% of tripping a watchdog
# that would have destroyed two hours of good work. The old margin was set
# for a faster model and a shorter prompt.
#
# The silent fact-check phase (batched greps over sys/, no log output)
# extends the quiet window further still, which is where all four
# 2026-08-22 kills actually landed.
WATCHDOG_SECS="${WATCHDOG_SECS:-2400}"

# Repo root. Everything below runs relative to it (.venv/bin/python,
# generate-doc.py), so a wrong value here fails in confusing ways much
# later. Resolution order:
#
#   1. $DAEMONDOCS_DIR, for the deployed copy, which lives in the queue
#      directory outside the repo and cannot derive anything from $0.
#   2. The parent of this script's own directory, correct whenever the
#      script is run from its scripts/ home in a checkout.
#
# Validated either way rather than trusted: a bare `cd` to a stale path
# would otherwise start the pipeline in the wrong tree. This replaced a
# hardcoded absolute checkout path, which also baked one operator's home
# directory into a public repo.
if [ -n "${DAEMONDOCS_DIR:-}" ]; then
    REPO_DIR="$DAEMONDOCS_DIR"
    REPO_SRC="env"
else
    REPO_DIR=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)
    REPO_SRC="script-relative"
fi
if [ -z "$REPO_DIR" ] || [ ! -f "$REPO_DIR/generate-doc.py" ]; then
    echo "$SELF_NAME: '$REPO_DIR' is not a DaemonDocs checkout (no generate-doc.py); set DAEMONDOCS_DIR" >&2
    exit 1
fi
cd "$REPO_DIR" || exit 1

# Cumulative decode-token counter from llama-server's /metrics, or empty
# if unavailable. Empty is treated by callers as "cannot confirm the model
# is working", which lets the watchdog kill — the failure mode it exists
# for (a local CPU spin with the endpoint idle) also reads as no decode
# progress, so failing safe means failing towards the kill.
# Requires llama-server started with --metrics.
METRICS_URL=$(echo "$URL" | sed -e 's#/v1/*$##')/metrics
fetch_decode_total() {
    fetch_out=$(fetch -q -T 8 -o - "$METRICS_URL" 2>/dev/null \
                || curl -s -m 8 "$METRICS_URL" 2>/dev/null)
    echo "$fetch_out" | awk '/^llamacpp:n_decode_total /{print $2; exit}'
}

# Routing alias for chat-completion requests. Ask the endpoint what it
# serves rather than assuming: llmsrv.sh gives each recipe its own alias
# (qwen38-mtp -> Qwen3.8-27B-Q8_0-MTP, qwen38-q8 -> Qwen3.8-27B-UD-Q8_K_XL),
# so a hardcoded value silently mismatches whenever the endpoint is
# restarted on the other slot. Explicit MODEL_ALIAS still wins, and the
# literal below is only reached when the endpoint cannot be queried at all.
# Same fetch-then-curl pattern as fetch_decode_total above.
MODELS_URL=$(echo "$URL" | sed -e 's#/*$##')/models
resolve_alias() {
    ra_out=$(fetch -q -T 8 -o - "$MODELS_URL" 2>/dev/null \
             || curl -s -m 8 "$MODELS_URL" 2>/dev/null)
    # First "id" under .data[] — llama-server serves exactly one model.
    # Plain sed so this needs no python/jq on the runner host.
    echo "$ra_out" \
      | tr ',{}' '\n\n\n' \
      | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      | head -1
}

if [ -z "${MODEL_ALIAS:-}" ]; then
    MODEL_ALIAS=$(resolve_alias)
    if [ -n "$MODEL_ALIAS" ]; then
        ALIAS_SRC="endpoint"
    else
        MODEL_ALIAS="Qwen3.8-27B-UD-Q8_K_XL"
        ALIAS_SRC="fallback-literal"
    fi
else
    ALIAS_SRC="env"
fi

echo "[$LABEL] starting at $(date -u +%FT%TZ) endpoint=$URL reviewer_thinking=$RTHINK rationale_enum=$RATENUM watchdog=${WATCHDOG_SECS}s recover=$RECOVER model_alias=$MODEL_ALIAS($ALIAS_SRC) repo=$REPO_DIR($REPO_SRC)" >> "$LOG"

# Startup inflight handling. A leftover file means the previous run of this
# label died between popping a chapter and finishing it.
if [ -s "$INFLIGHT" ]; then
    STALE=$(tr -d '[:space:]' < "$INFLIGHT")
    if [ "$RECOVER" = "1" ]; then
        # Push back onto the FRONT: an interrupted chapter is older work
        # than anything still queued, and the queue is in chapter order.
        lockf -s -t 30 "$LOCK" sh -c '
            printf "%s\n" "'"$STALE"'" > "'"$QUEUE"'.recover"
            cat "'"$QUEUE"'" >> "'"$QUEUE"'.recover"
            mv "'"$QUEUE"'.recover" "'"$QUEUE"'"
        '
        rm -f "$INFLIGHT" "$INFLIGHT.tmp"
        echo "[$LABEL] $(date -u +%FT%TZ) recovered chapter $STALE from $INFLIGHT; re-queued at front" >> "$LOG"
    else
        # Do not requeue: another runner with this label may be alive and
        # working on that very chapter. Requeuing here would double-generate.
        #
        # But DO move the evidence aside. $INFLIGHT is about to be reused as
        # this run's live claim, and the per-chapter `rm -f` release would
        # delete the crashed run's number as soon as the first chapter
        # finished — silently destroying the thing --recover needs and
        # making the whole recovery path unreachable. Preserve it under
        # .stale, which nothing in the claim/release cycle touches.
        mv "$INFLIGHT" "$INFLIGHT.stale"
        echo "[$LABEL] $(date -u +%FT%TZ) WARNING: stale inflight chapter $STALE preserved as $INFLIGHT.stale; NOT re-queued. Confirm no runner is alive (pgrep -f \"runner.sh $LABEL\"), then re-queue it with: printf '$STALE\\n' | cat - $QUEUE | sponge $QUEUE  (or restart with --recover)" >> "$LOG"
    fi
fi

# --recover also honours a previously preserved .stale record, so the
# documented two-step (plain restart warns, then restart with --recover)
# actually works.
if [ "$RECOVER" = "1" ] && [ -s "$INFLIGHT.stale" ]; then
    STALE=$(tr -d '[:space:]' < "$INFLIGHT.stale")
    lockf -s -t 30 "$LOCK" sh -c '
        printf "%s\n" "'"$STALE"'" > "'"$QUEUE"'.recover"
        cat "'"$QUEUE"'" >> "'"$QUEUE"'.recover"
        mv "'"$QUEUE"'.recover" "'"$QUEUE"'"
    '
    rm -f "$INFLIGHT.stale"
    echo "[$LABEL] $(date -u +%FT%TZ) recovered chapter $STALE from $INFLIGHT.stale; re-queued at front" >> "$LOG"
fi

while :; do
    # Atomic pop-and-claim: lockf serializes; sed -i deletes the first
    # line; the same critical section records the number in the inflight
    # file. Claiming inside the lock is the whole point — a crash between
    # the delete and the claim would lose the chapter, which is the bug
    # this file exists to prevent.
    # Note the guard runs BEFORE any redirection at $INFLIGHT. Writing
    # `... > "$INFLIGHT"` guarded by a test truncates the file even when
    # the test fails, because the shell opens the target first — that
    # wiped the stale record on every plain restart, destroying exactly
    # the number `--recover` needs. Write via a temp file and mv instead.
    N=$(lockf -s -t 30 "$LOCK" sh -c '
        n=$(head -n 1 "'"$QUEUE"'")
        sed -i "" -e "1d" "'"$QUEUE"'"
        if [ -n "$(echo "$n" | tr -d "[:space:]")" ]; then
            printf "%s\n" "$n" > "'"$INFLIGHT"'.tmp"
            mv "'"$INFLIGHT"'.tmp" "'"$INFLIGHT"'"
        fi
        printf "%s\n" "$n"
    ')
    # Strip whitespace.
    N=$(echo "$N" | tr -d '[:space:]')
    if [ -z "$N" ]; then
        echo "[$LABEL] queue empty at $(date -u +%FT%TZ); exiting" >> "$LOG"
        break
    fi

    CHLOG="$QUEUE_DIR/$LABEL-ch$N.log"
    : > "$CHLOG"   # truncate so we start the watchdog clock fresh

    echo "[$LABEL] $(date -u +%FT%TZ) starting chapter $N" >> "$LOG"

    # Launch python in the background so we can supervise it.
    # Wall-clock start, so the summary line can report per-chapter duration.
    t0=$(date +%s)

    OPENAI_BASE_URL="$URL" \
    OPENAI_MODEL="$MODEL_ALIAS" \
    DAEMONDOCS_WRITER_THINKING=1 \
    DAEMONDOCS_REVIEWER_THINKING="$RTHINK" \
    DAEMONDOCS_RATIONALE_ENUM="$RATENUM" \
        .venv/bin/python generate-doc.py --force --chapter "$N" \
            > "$CHLOG" 2>&1 &
    PYPID=$!

    # Watchdog loop: poll the chapter log's mtime every 60s. If the log
    # hasn't grown for WATCHDOG_SECS, kill python.
    last_mtime=$(stat -f %m "$CHLOG" 2>/dev/null || echo 0)
    last_change=$(date +%s)
    killed_by_watchdog=0

    while kill -0 "$PYPID" 2>/dev/null; do
        sleep 60
        # If python exited during the sleep, break out and let wait collect.
        kill -0 "$PYPID" 2>/dev/null || break

        cur_mtime=$(stat -f %m "$CHLOG" 2>/dev/null || echo 0)
        now=$(date +%s)

        if [ "$cur_mtime" != "$last_mtime" ]; then
            last_mtime="$cur_mtime"
            last_change="$now"
            continue
        fi

        idle=$(( now - last_change ))
        if [ "$idle" -ge "$WATCHDOG_SECS" ]; then
            # Before killing, ask the endpoint whether the MODEL is still
            # working. A quiet log does NOT mean a stuck process: this
            # hardware decodes at ~7 tok/s, so one long step is many
            # minutes of silence. On 2026-08-23 this watchdog killed a
            # perfectly healthy ch3 at step 11 — every step had run
            # 27-110s, the token cap had eliminated the real runaways, and
            # the in-process hang detector had (correctly) stayed silent
            # because the model was decoding. The dumb guard overrode the
            # smart one and destroyed 51 minutes of good work.
            #
            # n_decode_total advances per token; sample it twice. Note
            # tokens_predicted_total is the WRONG counter — it only rolls
            # up when a request completes, so it sits frozen through the
            # generation you are trying to observe.
            D1=$(fetch_decode_total)
            sleep 10
            D2=$(fetch_decode_total)
            if [ -n "$D1" ] && [ -n "$D2" ] && [ "$D1" != "$D2" ]; then
                echo "[$LABEL] $(date -u +%FT%TZ) watchdog: chapter $N log idle ${idle}s but endpoint still decoding ($D1 -> $D2); NOT killing" >> "$LOG"
                # Credit the model's progress so we re-arm rather than
                # re-checking every 60s for the rest of the chapter.
                last_change=$(date +%s)
                continue
            fi
            echo "[$LABEL] $(date -u +%FT%TZ) watchdog: chapter $N log idle ${idle}s (>= ${WATCHDOG_SECS}s), endpoint not decoding; killing pid $PYPID" >> "$LOG"
            kill "$PYPID" 2>/dev/null
            # Give python 10s to clean up, then SIGKILL if still alive.
            sleep 10
            kill -0 "$PYPID" 2>/dev/null && kill -9 "$PYPID" 2>/dev/null
            killed_by_watchdog=1
            break
        fi
    done

    wait "$PYPID" 2>/dev/null
    rc=$?
    secs=$(( $(date +%s) - t0 ))

    # Release the claim. All three outcomes — success, non-zero rc, and
    # watchdog kill — converge here, and all three mean "no longer in
    # flight". A failed chapter must NOT stay claimed: it is already gone
    # from the queue, and leaving the file behind would make the next
    # --recover re-run a chapter that had genuinely finished its attempt.
    rm -f "$INFLIGHT" "$INFLIGHT.tmp"

    # Circuit breaker. generate-doc.py now exits non-zero when it produced
    # no chapter. A persistent config error fails every chapter in ~60s, so
    # without this the runner silently drains all 38 queue entries. Two
    # consecutive failures means the problem is not chapter-specific: stop
    # and leave the rest of the queue intact for a human to look at.
    if [ "$rc" -ne 0 ]; then
        consec_fail=$(( ${consec_fail:-0} + 1 ))
    else
        consec_fail=0
    fi
    if [ "$killed_by_watchdog" = "1" ]; then
        echo "[$LABEL] $(date -u +%FT%TZ) finished chapter $N rc=$rc dur=${secs}s (KILLED BY WATCHDOG)" >> "$LOG"
    else
        echo "[$LABEL] $(date -u +%FT%TZ) finished chapter $N rc=$rc dur=${secs}s" >> "$LOG"
    fi

    if [ "${consec_fail:-0}" -ge 2 ]; then
        echo "[$LABEL] $(date -u +%FT%TZ) ABORT: $consec_fail consecutive failures; $(wc -l < "$QUEUE" | tr -d ' ') chapters left in queue" >> "$LOG"
        break
    fi
done
