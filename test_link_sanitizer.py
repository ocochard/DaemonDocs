#!/usr/bin/env python3
"""Smoke test for the chapter-link sanitizer.

Run on framework: `python3 test_link_sanitizer.py`.
Exits non-zero on any test failure.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "gendoc", os.path.join(HERE, "generate-doc.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []


def check(label, cond, info=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if info:
        print(f"         {info}")
    if not cond:
        failures.append(label)


# Simulate a small corpus.
CHAPTERS = {
    "README_internals.md",
    "sys/README.md",
    "sys/kern/README_locking.md",
    "sys/kern/README_process.md",
    "sys/kern/README_intr.md",
    "sys/kern/README_jail.md",
    "sys/vm/README.md",
    "sys/vm/README_bcache.md",
    "sys/dev/README_nic_drivers.md",
    "sys/net/README_vimage.md",
    "sys/sys/README_devfs.md",
    "sys/sys/README_rctl.md",
    "sys/geom/README.md",
    "sys/fs/README.md",
}

# We need SRC_ROOT to be a directory where these files don't exist on
# disk (so the sanitizer falls back to the chapter set, not stat()).
mod.SRC_ROOT = "/nonexistent_root_for_test"

# 1) The exact bug from the user's report: sys/kern/README_locking.md has
#    a "vm/README_bcache.md" link that resolves to sys/kern/vm/... (wrong).
#    Note: `vm/README.md` is ambiguous (multiple chapters share basename
#    `README.md`), so we expect the sanitizer to drop that line rather
#    than guess. Uniquely-named targets get rewritten.
print("Test 1: real-corpus bug — vm/README_bcache.md from sys/kern/")
content = """\
# Locking Primitives

## See Also
- [Process Management](kern/README_process.md)
- [Virtual Memory Subsystem](vm/README.md)
- [Buffer Cache](vm/README_bcache.md)
- `sys/kern/kern_mutex.c`
"""
out, rew, drop = mod._sanitize_chapter_links(
    content, "sys/kern/README_locking.md", CHAPTERS,
)
check("rewrote 3 broken links via tail-2 disambiguation",
      rew == 3, f"rewrote={rew}, dropped={drop}")
check("dropped nothing (tail-2 disambiguator finds vm/README.md)",
      drop == 0)
check("vm/README_bcache.md → ../vm/README_bcache.md",
      "(../vm/README_bcache.md)" in out, info=out)
check("kern/README_process.md → README_process.md",
      "(README_process.md)" in out)
check("vm/README.md → ../vm/README.md (tail-2 wins)",
      "(../vm/README.md)" in out)
check("non-link list-item preserved",
      "`sys/kern/kern_mutex.c`" in out)

# 2) Already-correct links pass through unchanged.
print("Test 2: clean See Also block is idempotent")
clean = """\
## See Also
- [Process](README_process.md)
- [VM](../vm/README.md)
"""
out2, rew2, drop2 = mod._sanitize_chapter_links(
    clean, "sys/kern/README_locking.md", CHAPTERS,
)
check("clean block unchanged", out2 == clean and rew2 == 0 and drop2 == 0)

# 3) Idempotent on already-fixed output.
print("Test 3: sanitizer is idempotent")
out3, rew3, drop3 = mod._sanitize_chapter_links(
    out, "sys/kern/README_locking.md", CHAPTERS,
)
check("second pass yields no changes",
      out3 == out and rew3 == 0 and drop3 == 0)

# 4) Truly ambiguous: bare `README.md` (no parent hint) when the corpus
#    has many. Both basename and tail-2 fail → drop the line.
print("Test 4: truly ambiguous link (bare README.md) — drop list item")
amb = """\
## See Also
- [Some readme](README.md)
"""
out4, rew4, drop4 = mod._sanitize_chapter_links(
    amb, "sys/kern/README_locking.md", CHAPTERS,
)
check("dropped the ambiguous list item", drop4 == 1 and rew4 == 0,
      f"rewrote={rew4}, dropped={drop4}, out={out4!r}")
check("'Some readme' line gone",
      "Some readme" not in out4, info=out4)

# 5) Inline-prose link with broken-but-uniquely-fixable target → rewrite.
print("Test 5: inline prose link gets rewritten when unique")
prose = "See [the buffer cache](vm/README_bcache.md) for details."
out5, rew5, drop5 = mod._sanitize_chapter_links(
    prose, "sys/kern/README_locking.md", CHAPTERS,
)
check("inline prose rewrite",
      "(../vm/README_bcache.md)" in out5 and rew5 == 1 and drop5 == 0,
      info=out5)
check("prose context preserved",
      "See " in out5 and " for details." in out5)

# 6) Inline-prose link with broken-AND-non-fixable target → leave alone
#    (we won't mangle a sentence by deleting just the link).
print("Test 6: inline prose link with no unique fix is left alone")
prose2 = "See [the missing thing](nowhere/README.md) somewhere."
out6, rew6, drop6 = mod._sanitize_chapter_links(
    prose2, "sys/kern/README_locking.md", CHAPTERS,
)
check("inline prose unchanged when ambiguous",
      out6 == prose2 and rew6 == 0 and drop6 == 0,
      info=out6)

# 7) http(s) and anchor-only links untouched.
print("Test 7: external/anchor links untouched")
ext = """\
## See Also
- [GitHub](https://github.com/freebsd/freebsd-src)
- [Top](#overview)
- [Mail](mailto:foo@example.com)
"""
out7, rew7, drop7 = mod._sanitize_chapter_links(
    ext, "sys/kern/README_locking.md", CHAPTERS,
)
check("external/anchor unchanged",
      out7 == ext and rew7 == 0 and drop7 == 0)

# 8) Anchor preserved on rewritten links.
print("Test 8: anchor fragment preserved on rewrite")
anchored = "- [BCache deep dive](vm/README_bcache.md#wakeup-path)"
out8, rew8, drop8 = mod._sanitize_chapter_links(
    anchored, "sys/kern/README_locking.md", CHAPTERS,
)
check("rewritten link keeps #anchor",
      "(../vm/README_bcache.md#wakeup-path)" in out8 and rew8 == 1,
      info=out8)

# 8b) Exact-duplicate See Also entries are deduped.
print("Test 8b: exact-duplicate list items in See Also are deduped")
dup = """\
# Locking

## See Also
- [Process](README_process.md)
- [Process](README_process.md)
- [VM](../vm/README.md)
- [Process](README_process.md)

## Notes
- [Process](README_process.md)
- [Process](README_process.md)
"""
out8b, rew8b, drop8b = mod._sanitize_chapter_links(
    dup, "sys/kern/README_locking.md", CHAPTERS,
)
check("dropped 2 duplicates inside See Also",
      drop8b == 2 and rew8b == 0,
      f"rewrote={rew8b}, dropped={drop8b}")
check("Notes section duplicates left untouched",
      out8b.count("- [Process](README_process.md)") == 1 + 2,
      f"out={out8b!r}")

# 9) End-to-end on the actual corpus on disk (if running on framework).
real_root = os.path.expanduser("~/freebsd-src")
real_index = os.path.join(real_root, "README.all-chapters.md")
if os.path.isfile(real_index):
    print(f"Test 9: end-to-end on real corpus at {real_root}")
    import re
    idx = open(real_index).read()
    real_chapters = set()
    for m in re.finditer(r"\(([^)\s]+\.md)\)", idx):
        rel = m.group(1)
        if rel.startswith("http"):
            continue
        real_chapters.add(rel)
    # Restore SRC_ROOT for the on-disk fallback in _is_chapter_target.
    mod.SRC_ROOT = real_root

    target_file = "sys/kern/README_locking.md"
    target_path = os.path.join(real_root, target_file)
    if target_file in real_chapters and os.path.isfile(target_path):
        text = open(target_path).read()
        fixed, rew9, drop9 = mod._sanitize_chapter_links(
            text, target_file, real_chapters,
        )
        check(
            "real README_locking.md gets fixed (or already clean)",
            (rew9 + drop9 > 0) or (
                "(vm/README_bcache.md)" not in text
                and "(kern/README_process.md)" not in text
            ),
            f"rewrote={rew9}, dropped={drop9}",
        )
        # And running again is a no-op.
        again, rew9b, drop9b = mod._sanitize_chapter_links(
            fixed, target_file, real_chapters,
        )
        check("second pass on real file is idempotent",
              again == fixed and rew9b == 0 and drop9b == 0)
    else:
        print(f"  (skipped — {target_file} not in corpus)")
else:
    print(f"(skipped Test 9 — {real_index} not present)")

print()
print("=" * 60)
if failures:
    print(f"FAILED: {len(failures)} test(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All tests passed.")
