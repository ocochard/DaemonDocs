#!/usr/bin/env python3
"""Regression tests for the 2026-08-27 stray-output-directory bug.

`output_file` is relative to the FreeBSD source root, so a value missing
its top-level prefix is still a perfectly valid relative path. ch17 (NFS)
had `output_file: "fs/nfs/README.md"` instead of `"sys/fs/nfs/README.md"`,
so a 3h24m chapter wrote a brand-new top-level `fs/nfs/` directory into
the source tree and never landed in `sys/fs/nfs/`, the code it documents.

Nothing downstream had reason to complain: `source_dirs` were correct, so
the CONTENT was about the real NFS implementation and every fact-check
passed. Only the destination was wrong.

Pinned here:
  1. run_chapter refuses an output_file whose parent dir does not exist,
     and refuses it BEFORE spending any model time;
  2. the error names the likely `sys/` correction rather than just failing;
  3. every chapter in the shipped chapters.yaml resolves to a real dir.

Run: `python3 test_output_path.py`. Exits non-zero on any failure.
"""
import contextlib
import importlib.util
import io
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)  # tests/ lives one level below the repo root
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


def run(output_file):
    """Call run_chapter far enough to hit the path guard.

    writer/reviewer are None on purpose: the guard must reject before
    either is touched, so a None here proves the ordering. If the guard
    ever regresses, the run reaches the writer and dies on None instead,
    which the "no model time spent" check below detects.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rv = mod.run_chapter({"title": "T", "output_file": output_file},
                             None, None, 1)
    return rv, buf.getvalue()


print("1) missing top-level prefix is rejected")

# Pick a relative path that is real under sys/ but not at the root, so the
# test does not depend on ch17 specifically.
probe = None
for cand in ("fs/nfs", "kern", "vm", "net"):
    if (os.path.isdir(os.path.join(mod.SRC_ROOT, "sys", cand))
            and not os.path.isdir(os.path.join(mod.SRC_ROOT, cand))):
        probe = cand
        break

if probe is None:
    print("  [SKIP] no suitable probe dir under SRC_ROOT "
          f"({mod.SRC_ROOT}); is the source tree present?")
else:
    rv, out = run(f"{probe}/README.md")
    check("run_chapter returns False", rv is False, f"got {rv!r}")
    check("error names the offending directory", f"{probe}/" in out)
    check("error suggests the sys/ correction",
          f"sys/{probe}/README.md" in out,
          "a bare failure makes the operator go hunting")
    check("refuses rather than creating the layout",
          "Refusing to create" in out)
    # The whole point of checking early: a typo must not cost a chapter.
    check("no model time spent before rejecting",
          "initial draft failed" not in out and "[draft]" not in out,
          "guard must run before the writer is invoked")
    # And it must not have created the directory as a side effect.
    check("does not create the bogus directory",
          not os.path.isdir(os.path.join(mod.SRC_ROOT, probe)))

print()
print("2) a valid output_file is not rejected")

# Any existing chapter dir proves the guard does not over-reject. Use the
# corrected NFS path when present, else fall back to any real sys/ dir.
valid = None
for cand in ("sys/fs/nfs/README.md", "sys/kern/README.md"):
    if os.path.isdir(os.path.dirname(os.path.join(mod.SRC_ROOT, cand))):
        valid = cand
        break
if valid is None:
    print("  [SKIP] no source tree to validate against")
else:
    _, out = run(valid)
    check("valid path passes the guard",
          "Refusing to create" not in out,
          f"{valid} -> reached generation (then failed on the None writer, "
          "which is expected here)")

print()
print("3) shipped chapters.yaml resolves")

cfg = yaml.safe_load(open(os.path.join(REPO, "chapters.yaml"),
                          encoding="utf-8"))
chapters = cfg["chapters"] if isinstance(cfg, dict) else cfg
unresolvable = []
for ch in chapters:
    of = ch.get("output_file", "README.md")
    parent = os.path.dirname(os.path.join(mod.SRC_ROOT, of))
    if parent and not os.path.isdir(parent):
        unresolvable.append((ch.get("title", "?"), of))

if not os.path.isdir(mod.SRC_ROOT):
    print(f"  [SKIP] SRC_ROOT {mod.SRC_ROOT} not present")
else:
    check(f"all {len(chapters)} chapters target an existing directory",
          not unresolvable,
          "; ".join(f"{t}: {p}" for t, p in unresolvable))

# Independent of the tree: a non-root output_file must carry a plausible
# top-level prefix. This catches the typo class even on a host with no
# source checkout.
tops = {of.split("/", 1)[0]
        for of in (c.get("output_file", "README.md") for c in chapters)
        if "/" in of}
suspicious = sorted(t for t in tops if t in {"fs", "kern", "net", "vm",
                                             "dev", "ufs", "geom", "cam"})
check("no chapter output_file starts with a kernel SUBdirectory name",
      not suspicious,
      f"{suspicious} look like they are missing a 'sys/' prefix")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("all output-path tests passed")
