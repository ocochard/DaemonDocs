#!/usr/bin/env python3
"""Tests for fail-closed grep-output-cap handling in the fact-checker.

The defect this pins (ch34, KDB/DDB, 2026-08-29): `_batched_grep_present`
ends its pipeline with `head -c <cap>`, which truncates mid-stream with no
error. A truncated read looked exactly like "grep ran, nothing matched",
so stage 3 found no definition for every symbol whose matches fell past
the cut, `_verify_with_cache` cached `False` for each, and
`build_review_prompt` handed the reviewer a fabricated "Missing functions"
list. The reviewer duly FAILed Accuracy on 15 real `sys/ddb/` functions
for three straight rounds and the chapter shipped UNVERIFIED after ~7h.

The trigger was upstream: `_extract_function_names` returned `int` as a
claimed function. `int` alone matches ~4.2 MB of stage-2 output on `sys/`
— 87% of a 20-symbol batch — so it evicted every real symbol.

Three layers are pinned here, because each alone is insufficient:
  1. junk candidates (C keywords, 1-2 char names) never reach grep;
  2. a truncated read returns None (never cached) instead of a short set;
  3. verification is chunked, and a chunk that trips the cap is retried
     per-symbol so innocent symbols still get a real verdict.

Run: `python3 test_grep_cap.py`. Exits non-zero on any failure.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "gendoc", os.path.join(REPO, "generate-doc.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")
    if not ok:
        failures.append(label)


print("1) junk candidates never reach grep")
# `int` is the one that actually broke ch34; the rest are the same class.
for junk in ("int", "void", "char", "unsigned", "sizeof", "return",
             "size_t", "uint32_t"):
    check(f"C keyword filtered: {junk}",
          mod._filter_known_noise([junk]) == [],
          f"got {mod._filter_known_noise([junk])}")
for short in ("O", "x", "i", "tv", "up"):
    check(f"short name filtered: {short}",
          mod._filter_known_noise([short]) == [],
          f"got {mod._filter_known_noise([short])}")
# Real symbols must survive the filter — the fix must not overreach.
real = ["db_command", "vm_page_insert", "kdb_enter", "malloc_domainset"]
check("real symbols survive the filter",
      mod._filter_known_noise(real) == real,
      f"got {mod._filter_known_noise(real)}")

print()
print("2) an extracted draft no longer yields `int` as a function")
draft = (
    "The `db_command()` dispatcher runs. A cast like `int(x)` is not a\n"
    "function, nor is `sizeof(foo)`. Calls `db_ps()` too.\n"
    "```c\n"
    "static int\n"
    "db_lookup(const char *symstr)\n"
    "{\n"
    "\treturn (0);\n"
    "}\n"
    "```\n"
)
got = set(mod._extract_function_names(draft))
check("`int` not extracted", "int" not in got, f"got {sorted(got)}")
check("`sizeof` not extracted", "sizeof" not in got)
check("`return` not extracted", "return" not in got)
check("real names still extracted",
      {"db_command", "db_ps", "db_lookup"} <= got, f"got {sorted(got)}")

print()
print("3) truncation is fail-closed, not a short read")
# Force the cap to a tiny value so any real grep trips it, and assert the
# reader returns None (retryable) rather than an empty set (cacheable).
saved = mod._GREP_OUTPUT_CAP_BYTES
try:
    mod._GREP_OUTPUT_CAP_BYTES = 64
    roots, _ = mod._resolve_search_roots(mod.SRC_ROOT, None)
    shape = (r"[A-Za-z_][A-Za-z0-9_]* *\*? *[A-Za-z_][A-Za-z0-9_]*\("
             r"|^[A-Za-z_][A-Za-z0-9_]*\(")
    pat = r"(?:(?:[A-Za-z_]\w*\s*\**\s+)+\*?\s*({alt})\s*\(|^({alt})\s*\()"
    res = mod._batched_grep_present(
        ["malloc"], pat, roots, shape)
    check("cap hit returns None, not a set", res is None, f"got {res!r}")
finally:
    mod._GREP_OUTPUT_CAP_BYTES = saved

print()
print("4) a None verdict is never cached")
mod._FACT_CHECK_CACHE.clear()
saved = mod._GREP_OUTPUT_CAP_BYTES
try:
    mod._GREP_OUTPUT_CAP_BYTES = 64
    mod._verify_functions(["db_command"], mod.SRC_ROOT, None)
    cached = [k for k in mod._FACT_CHECK_CACHE if k[2] == "db_command"]
    check("no cache entry written after a cap hit", not cached,
          f"cache keys: {cached}")
finally:
    mod._GREP_OUTPUT_CAP_BYTES = saved
    mod._FACT_CHECK_CACHE.clear()

print()
print("5) the ch34 regression: real sys/ddb functions verify")
# The exact 15 names the live run reported NOT FOUND, plus the 4 that
# genuinely are absent from `sys/` (userland tools and sysctl names).
# This is the assertion that would have caught the bug.
real_ddb = ["db_command", "db_command_loop", "db_command_register",
            "db_examine", "db_examine_cmd", "db_lookup", "db_printsym",
            "db_ps", "db_ps_proc", "db_put_value", "db_read_line",
            "db_stack_trace", "db_stack_trace_active",
            "db_stack_trace_all", "dumpthread"]
absent = ["dumpdev", "dumpon", "kgdb", "savecore"]
if os.path.isdir(os.path.join(mod.SRC_ROOT, "sys", "ddb")):
    mod._FACT_CHECK_CACHE.clear()
    # Pad the batch well past one chunk so chunking + retry are exercised.
    pad = ["malloc", "free", "printf", "panic", "uma_zalloc", "vm_page_alloc",
           "lock", "kdb_enter", "vpanic", "bzero", "memcpy", "sbuf_new",
           "taskqueue_enqueue", "callout_reset", "sysctl_handle_int"]
    batch = real_ddb + absent + pad
    missing = set(mod._verify_functions(batch, mod.SRC_ROOT, None))
    bad = sorted(missing & set(real_ddb))
    check("no real sys/ddb function reported missing", not bad,
          f"falsely missing: {bad}" if bad
          else f"all {len(real_ddb)} verified in a {len(batch)}-symbol batch")
    check("genuinely absent names still reported",
          set(absent) <= missing,
          f"missing set: {sorted(missing)}")
    mod._FACT_CHECK_CACHE.clear()
else:
    print(f"  [SKIP] {mod.SRC_ROOT}/sys/ddb not present")

print()
print("6) chunk size and caps are coherent")
check("chunk size is positive", mod._VERIFY_CHUNK_SIZE > 0,
      f"got {mod._VERIFY_CHUNK_SIZE}")
check("small cap <= main cap",
      mod._GREP_OUTPUT_CAP_SMALL_BYTES <= mod._GREP_OUTPUT_CAP_BYTES,
      f"{mod._GREP_OUTPUT_CAP_SMALL_BYTES} vs {mod._GREP_OUTPUT_CAP_BYTES}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("all grep-cap tests passed")
