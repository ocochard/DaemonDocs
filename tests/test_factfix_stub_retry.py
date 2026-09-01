#!/usr/bin/env python3
"""Tests for the fact-fix stub-retry (added 2026-09-01).

Motivating defect (ch21, pf, run finished 2026-08-27T14:58Z): the
reviewer graded the chapter PASS 8/8, then the fact-check pass found
real fabrications -- DTrace probes absent from every SDT_PROBE_DEFINE*
macro, a hallucinated `pf_state_cmp` sub-struct, a suspect `net.pfil`
sysctl. The fact-fix rewrite was attempted, its output tripped
`_looks_like_stub`, and the pipeline logged

    fact-fix: output looks truncated/stub -- keeping pre-fact-fix draft

so the chapter SHIPPED with every named hallucination still in it,
flagged only by an UNVERIFIED banner.

The initial-draft stage already retried once on a stub, with a pointed
`final_answer()` reminder; revision and fact-fix deliberately did not
(FUTURE_IMPROVEMENTS, "Stub-retry on initial draft"). That asymmetry is
right for a REVISION -- the fallback there is merely less-polished
prose. It is wrong for FACT-FIX, where the fallback is known-false
content. This adds the same one-shot retry to fact-fix only.

The retry must stay bounded and fail safe: exactly one extra call, and
if it also stubs, keep the pre-fact-fix draft and set
`fact_fix_failed` -- byte-identical to the old behaviour, so the banner
still appears.

What is pinned here:
  1. the retry exists and runs as its own named stage;
  2. the retry prompt is built from the fact-check prompt, so the
     flagged symbols are still in front of the writer;
  3. the retry's own output is stub-checked (a stub retry must not be
     accepted as a fix);
  4. a successful retry replaces the draft;
  5. a stubbed retry keeps the pre-fact-fix draft AND sets
     fact_fix_failed -- the banner path is preserved;
  6. an exception inside the retry cannot escape the fact-fix block;
  7. it is ONE retry, not a loop (bounded cost);
  8. a non-stub first attempt is unaffected -- no extra call.

Run: `python3 test_factfix_stub_retry.py`. Exits non-zero on failure.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = open(os.path.join(REPO, "generate-doc.py"), encoding="utf-8").read()

failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"         {detail}")
        failures.append(label)


# Isolate the fact-fix block: from the "rewriting" banner to the
# "all claims verified" else-arm that closes it.
m = re.search(
    r'print\("  \[fact-fix\] rewriting.*?fact_fix_failed = True\n'
    r'(?=\s*else:\n\s*print\("         all claims)',
    SRC, re.S)
check("fact-fix block is locatable", m is not None)
if m is None:
    print("\nCannot continue without the block.")
    sys.exit(1)
blk = m.group(0)

print("\nfact-fix stub retry:")
check("retry runs as its own named stage",
      blk.count('"fact-fix-retry"') == 1,
      f'occurrences={blk.count(chr(34)+"fact-fix-retry"+chr(34))}')
check("retry prompt derives from the fact-check prompt",
      "fact_retry_prompt" in blk and "fact_prompt" in blk)
check("retry prompt names final_answer",
      "final_answer" in blk)
check("retry output is itself stub-checked",
      blk.count("_looks_like_stub") == 2,
      f"expected 2, got {blk.count('_looks_like_stub')}")
check("successful retry replaces the draft",
      "draft = retry_draft" in blk)
check("empty retry result is treated as failure",
      "if retry_draft and not _looks_like_stub(retry_draft)" in blk)

print("\nfail-safe behaviour preserved:")
check("stubbed retry still sets fact_fix_failed (banner path)",
      blk.count("fact_fix_failed = True") >= 2,
      f"expected >=2, got {blk.count('fact_fix_failed = True')}")
check("retry exception is caught, not propagated",
      "except Exception" in blk and 'retry_draft = ""' in blk)
check("non-stub first attempt still assigns directly",
      re.search(r"else:\s*\n\s*draft = new_draft", blk) is not None)

print("\nbounded cost:")
check("exactly one retry, not a loop",
      blk.count("fact-fix-retry") == 1 and "while " not in blk)
check("retry is inside the not-clean branch only",
      "fact_check_clean" not in blk)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED")
    sys.exit(1)
print("all checks passed")
