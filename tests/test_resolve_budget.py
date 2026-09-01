#!/usr/bin/env python3
"""resolve_c_definition must terminate, and must not lie when it is cut short.

Two defects, found together while diagnosing the ch38 wedge (2026-09-01).

**1. `_FUNC_DECL_RE` did not terminate.** Its argument-list groups were
`(?:[^{;=()]|\n)*?`. A negated character class already matches newline, so
the `|\n` branch was redundant -- and catastrophic: every newline had two
ways to match, so the engine explored 2^n paths across n lines. On
`sys/arm64/arm64/vfp.c`, an ordinary 32 KB file, `_extract_func_sigs` ran
past 25s with no bound. The hang detector's ch38 dump pointed at the
resolve_c_definition tree walk, which is genuinely slow but finishes in
16.2s; the walk was a red herring and this regex was the wedge.

Note a length bound is NOT an acceptable alternative fix: `{0,400}`
terminates but silently drops real declarations with long argument lists
(four of them in `sys/dev/pms/.../saproto.h`), and `{0,800}` reintroduces
the hang. No cap both terminates and keeps every real match.

**2. The three tree walks had no ceiling.** `resolve_c_definition` walks
sys/ up to three times (~15200 files, ~357 MB). The old "limit" in the
fallback walk was `files[:50]` per directory, which bounded nothing --
all 3315 directories were still visited -- while making 2872 of 15221
source files (19%) permanently invisible to it.

The subtle requirement is the honesty one: a search stopped by the budget
must NOT report "Could not find definition", because that string is a
positive claim of absence the writer turns into prose. It must say it was
truncated. Equally, the budget must be loose enough that a *complete*
negative search still returns the real "Could not find definition" --
measured at 23.0s, so a 20s default made every genuine negative look
unresolved. That is why the default is 45s.
"""
import importlib.util
import os
import re
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "gendoc", os.path.join(REPO, "generate-doc.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

SRC = os.path.expanduser("~/freebsd-src")
HAVE_SRC = os.path.isdir(os.path.join(SRC, "sys"))
failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")
    if not ok:
        failures.append(label)


class _Timeout(Exception):
    pass


def _alarm(_sig, _frm):
    raise _Timeout()


print("1) the newline alternation is gone from _FUNC_DECL_RE")
_pat = mod._FUNC_DECL_RE.pattern
check("no `|\\n` alternation in the arg-list groups",
      "[^{;=()]|\\n" not in _pat and "(?:[^{;=()]|\n)" not in _pat,
      "reintroducing it makes _extract_func_sigs unbounded")
check("arg lists still use the negated class",
      "[^{;=()]*?" in _pat)
# A bound is the other tempting "fix"; it drops real declarations.
check("no length bound was substituted for the real fix",
      not re.search(r"\[\^\{;=\(\)\]\{0,\d+\}", _pat),
      "{0,400} drops long real declarations; {0,800} rehangs")

print("2) the regex terminates on the file that wedged ch38")
if not HAVE_SRC:
    check("skipped (no ~/freebsd-src)", True)
else:
    vfp = os.path.join(SRC, "sys/arm64/arm64/vfp.c")
    if not os.path.exists(vfp):
        check("skipped (vfp.c absent)", True)
    else:
        from pathlib import Path
        txt = Path(vfp).read_text(errors="ignore")
        # SIGALRM, not a bare call: against the broken pattern this does not
        # return, and a regression test for a hang must FAIL rather than
        # hang -- otherwise reverting the fix wedges the whole suite instead
        # of reporting it. re has no timeout, so only a signal can cut a
        # match short (same reason the hang detector uses faulthandler).
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(10)
        t0 = time.time()
        try:
            sigs = mod._extract_func_sigs(txt, "sys/arm64/arm64/vfp.c")
            signal.alarm(0)
            dur = time.time() - t0
            # The fixed pattern does this in ~0.002s. Anything near a second
            # means the exponential is back.
            check("_extract_func_sigs(vfp.c) completes under 1s",
                  dur < 1.0, f"{dur:.3f}s, {len(sigs)} signatures")
            check("and still extracts signatures from it",
                  len(sigs) > 10, f"{len(sigs)} found")
        except _Timeout:
            signal.alarm(0)
            check("_extract_func_sigs(vfp.c) completes under 1s", False,
                  "did not return within 10s -- catastrophic backtracking")
            check("and still extracts signatures from it", False,
                  "unreachable: the call never returned")

print("3) the redundancy claim is actually true")
# The whole fix rests on this equivalence; pin it so nobody 'restores'
# the newline branch for readability.
check("a negated class matches newline",
      bool(re.match(r"[^{;=()]", "\n")))
_a = re.compile(r"\A(?:[^{;=()]|\n)*\Z")
_b = re.compile(r"\A[^{;=()]*\Z")
_alpha = "a;=(){}\n\t *,\\"
import random
random.seed(3)
_bad = None
for _ in range(20000):
    s = "".join(random.choice(_alpha) for _ in range(random.randint(0, 8)))
    if bool(_a.match(s)) != bool(_b.match(s)):
        _bad = s
        break
check("both forms accept the same language", _bad is None,
      "differs on " + repr(_bad) if _bad else "20k random strings agree")

print("4) declaration classes the tool exists to find still resolve")
_want = {
    "sys/netinet/tcp_var.h": ["cc_conn_init", "cc_post_recovery"],
    "sys/sys/sysent.h": ["sv_fetch_syscall_args"],
    "sys/vm/vm_pager.h": ["pgo_getpages"],
}
if not HAVE_SRC:
    check("skipped (no ~/freebsd-src)", True)
else:
    from pathlib import Path
    for rel, syms in _want.items():
        p = os.path.join(SRC, rel)
        if not os.path.exists(p):
            check(f"skipped ({rel} absent)", True)
            continue
        got = {s.split(" ", 1)[0]
               for s in mod._extract_func_sigs(
                   Path(p).read_text(errors="ignore"), rel)}
        for sym in syms:
            check(f"{sym} still found in {rel}", sym in got)
    # the negative half: these do not exist and must stay unfound
    _neg = set()
    for rel in _want:
        p = os.path.join(SRC, rel)
        if os.path.exists(p):
            _neg |= {s.split(" ", 1)[0]
                     for s in mod._extract_func_sigs(
                         Path(p).read_text(errors="ignore"), rel)}
    for sym in ("cc_record_rtt", "cc_rttsample", "cc_newround"):
        check(f"{sym} correctly absent", sym not in _neg)

print("5) the walk budget exists and is env-tunable")
_b = getattr(mod, "_RESOLVE_WALK_BUDGET_SEC", None)
check("_RESOLVE_WALK_BUDGET_SEC is defined", _b is not None)
if _b is not None:
    # A complete negative search measured 23.0s. Below that, every genuine
    # "not found" degrades into a false "unresolved".
    check("default leaves headroom over a complete negative search (23.0s)",
          _b >= 30.0, f"default={_b}s")

print("6) the fallback walk no longer samples files[:50]")
_src = open(os.path.join(REPO, "generate-doc.py")).read()
check("files[:50] sampling cap removed",
      "for fname in files[:50]" not in _src,
      "it hid 2872 of 15221 source files while bounding nothing")

print("7) a truncated search must not claim the symbol is absent")
if not HAVE_SRC:
    check("skipped (no ~/freebsd-src)", True)
else:
    _saved = mod._RESOLVE_WALK_BUDGET_SEC
    try:
        # Tight ceiling so the walk is certainly cut short.
        mod._RESOLVE_WALK_BUDGET_SEC = 2.0
        out = mod.ResolveCDefinition().forward(symbol="cc_record_rtt")
    finally:
        mod._RESOLVE_WALK_BUDGET_SEC = _saved
    check("truncated result does not say 'Could not find definition'",
          "Could not find definition" not in out,
          "that string is a positive claim of absence")
    check("truncated result says so explicitly",
          "budget" in out.lower(),
          out.split("\n")[0][:70])
    check("and marks the symbol unresolved, not missing",
          "NOT evidence" in out or "unresolved" in out.lower())

print("8) a complete search still returns the exact legacy string")
if not HAVE_SRC:
    check("skipped (no ~/freebsd-src)", True)
else:
    _saved = mod._RESOLVE_WALK_BUDGET_SEC
    try:
        mod._RESOLVE_WALK_BUDGET_SEC = 0.0  # disabled: never truncates
        out = mod.ResolveCDefinition().forward(symbol="cc_record_rtt")
    finally:
        mod._RESOLVE_WALK_BUDGET_SEC = _saved
    check("uncapped miss returns 'Could not find definition'",
          out.startswith("Could not find definition"),
          out[:70])

print("9) forward() still returns a plain string")
# _resolve returns (text, truncated); forward must unwrap it. A tuple
# leaking to the agent would be a silently useless tool output.
if not HAVE_SRC:
    check("skipped (no ~/freebsd-src)", True)
else:
    out = mod.ResolveCDefinition().forward(symbol="struct vm_page")
    check("forward returns str, not tuple", isinstance(out, str),
          type(out).__name__)
    check("and resolved the struct", "vm_page" in out)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")
