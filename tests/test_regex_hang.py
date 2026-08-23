#!/usr/bin/env python3
"""Regression tests for the 2026-08-22 fact-check hang.

`_FENCED_FUNC_DEF_RE` backtracked catastrophically on a real ch3 draft:
one CPU at 99% for 6.8 hours, LLM endpoint idle, no log output, on a
chapter the writer had ALREADY finished. Every other extractor handled
the same 20KB draft in under 0.1s.

Two things are pinned here:
  1. the regex is linear on the input that hung it, and still matches
     every real definition shape it matched before;
  2. the hang detector fires and names the phase, so the next hang
     anywhere in the pipeline costs minutes to diagnose, not hours.

Run: `python3 test_regex_hang.py`. Exits non-zero on any failure.
"""
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)  # tests/ lives one level below the repo root
spec = importlib.util.spec_from_file_location(
    "gendoc", os.path.join(REPO, "generate-doc.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []


def check(label, cond, info=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if info:
        print(f"         {info}")
    if not cond:
        failures.append(label)


# --- 1) the exact input that hung, if it is still around ------------------
print("1) catastrophic backtracking")

# Committed fixture, not a /tmp path — a test that silently SKIPs after a
# reboot is not a regression test.
REPRO = os.path.join(HERE, "testdata", "ch3-fenced-hang.md")
draft = open(REPRO, encoding="utf-8", errors="ignore").read()
t = time.time()
mod._extract_fenced_function_defs(draft)
dt = time.time() - t
check("real ch3 draft scans in <2s", dt < 2.0, f"{dt:.3f}s ({len(draft)}B)")

# A synthetic worst case: many type-like tokens on a line whose trailing
# `{` never arrives, which is what makes the engine re-split them.
evil = "```c\n" + ("static const unsigned long int volatile " * 12) + "x(a\n```"
t = time.time()
mod._extract_fenced_function_defs(evil)
dt = time.time() - t
check("synthetic worst case scans in <2s", dt < 2.0, f"{dt:.3f}s")

print()

# --- 2) behaviour preserved ----------------------------------------------
print("2) real definitions still detected")

CASES = [
    ("simple", "```c\nint foo(void)\n{\n}\n```", {"foo"}),
    ("static + pointer", "```c\nstatic struct proc *bar(int a)\n{\n}\n```", {"bar"}),
    ("K&R newline", "```c\nstatic int\nbaz(void)\n{\n}\n```", {"baz"}),
    ("gcc attribute", "```c\nstatic void qux(void) __dead2\n{\n}\n```", {"qux"}),
    ("long type chain",
     "```c\nstatic const volatile unsigned long int *deep(void)\n{\n}\n```",
     {"deep"}),
    ("prototype not matched", "```c\nint notdef(void);\n```", set()),
    ("call site not matched", "```c\nx = compute(a, b);\n```", set()),
]
for label, src, want in CASES:
    got = set(mod._extract_fenced_function_defs(src))
    check(f"{label}", got == want, f"got={sorted(got)} want={sorted(want)}")

print()

# --- 3) the hang detector itself -----------------------------------------
print("3) hang detector")

check("beat() and heartbeat() exist",
      callable(mod.beat) and callable(mod.heartbeat))

# heartbeat() must restore the previous phase label on exit, or every
# later dump is attributed to whichever phase happened to run last.
mod.beat("outer")
with mod.heartbeat("inner"):
    inner = mod._hb_label
after = mod._hb_label
check("heartbeat restores the previous phase",
      inner == "inner" and after == "outer",
      f"inner={inner!r} after={after!r}")

# A dump must be reachable without a live hang: verify the watchdog's
# quiet-time arithmetic rather than sleeping through a real one.
mod.beat("probe")
quiet_now = time.monotonic() - mod._hb_last
check("beat() resets the quiet timer", quiet_now < 1.0, f"{quiet_now:.3f}s")

check("detector is disablable",
      os.environ.get("DAEMONDOCS_HANG_DUMP_SEC") is not None
      or mod._HANG_DUMP_AFTER_SEC > 0,
      "DAEMONDOCS_HANG_DUMP_SEC=0 turns it off")

# The first version used 300s and fired on a healthy draft: a reasoning
# model can think for many minutes before its first token, and a single
# legitimate step has been measured at 1164s. A detector that cries wolf
# gets ignored, so the threshold must stay above real LLM latency. The
# case it exists for ran 6.8 HOURS — there is no need to sit close.
if os.environ.get("DAEMONDOCS_HANG_DUMP_SEC") is None:
    check("quiet threshold is above observed legitimate step latency",
          mod._HANG_DUMP_AFTER_SEC >= 1500,
          f"{mod._HANG_DUMP_AFTER_SEC}s (slowest real step seen: 1164s)")

print()

# --- 4) endpoint-liveness probe ------------------------------------------
print("4) endpoint liveness (metrics-based)")

# The probe exists so a slow model is not mistaken for a hang. Its
# fail-safe direction is the load-bearing property: ANY uncertainty must
# read as "not decoding" so the detector still dumps. The bug it must
# never mask is a local CPU spin with the endpoint idle — which looks
# exactly like a flat decode counter.
saved = mod.MODEL_CONFIG["api_base"]
try:
    mod.MODEL_CONFIG["api_base"] = "http://127.0.0.1:1/v1"  # nothing listens
    check("unreachable endpoint -> count is None",
          mod._endpoint_decode_count() is None)
    check("unreachable endpoint -> NOT decoding (detector still fires)",
          mod._endpoint_is_decoding() is False)
finally:
    mod.MODEL_CONFIG["api_base"] = saved

# tokens_predicted_total only rolls up when a request COMPLETES, so it
# sits frozen for the whole generation you are trying to observe.
# n_decode_total advances per token. Using the wrong one silently
# reinstates the false positives this probe was written to remove.
import inspect
src = inspect.getsource(mod._endpoint_decode_count)
check("probe reads n_decode_total, not tokens_predicted_total",
      "n_decode_total" in src and "tokens_predicted_total" not in src)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("all regex-hang tests passed")
