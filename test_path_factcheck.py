#!/usr/bin/env python3
"""Tests for directory-aware path fact-checking.

Before 2026-08-22 `_extract_file_paths` required a file extension, so
directory claims were never extracted and never verified. ch1 (Source Tree
— almost entirely directory prose) shipped 9 nonexistent paths while
fact-check reported clean, including `gnu/`, a subtree FreeBSD had
retired upstream.

These tests pin both halves of the fix:
  - directories and extensionless files ARE now caught
  - the shapes that are legitimately absent from a source checkout are
    NOT flagged (a noisy verifier burns the writer's fact-fix budget,
    which has caused regressions before)

Run: `python3 test_path_factcheck.py`. Exits non-zero on any failure.
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

SRC = os.path.expanduser(os.environ.get("FREEBSD_SRC", "~/freebsd-src"))
print(f"FREEBSD_SRC = {SRC}")
print()

failures = []


def check(label, cond, info=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if info:
        print(f"         {info}")
    if not cond:
        failures.append(label)


def flagged(text):
    """Return the set of paths fact-check would report for `text`."""
    paths = mod._extract_file_paths(text)
    return {m.split(" → ")[0] for m in mod._verify_file_paths(paths, SRC)}


# --- 1) directory claims are extracted at all -----------------------------
print("1) directory + extensionless extraction")

got = mod._extract_file_paths("The `gnu/` subtree and `librescue/` live here.")
check("trailing-slash dirs extracted", {"gnu", "librescue"} <= set(got), str(got))

got = mod._extract_file_paths("See `gnu/Makefile` and `gnu/COPYING`.")
check("extensionless files extracted",
      {"gnu/Makefile", "gnu/COPYING"} <= set(got), str(got))

got = mod._extract_file_paths("Tests live in `src/tests/lib/libc`.")
check("multi-segment dir extracted", "src/tests/lib/libc" in got, str(got))

print()

# --- 2) retired / fabricated paths are flagged ----------------------------
print("2) nonexistent paths are flagged")

# gnu/ was retired upstream (134a4c78d070 "Retire the GNU subtree") and is
# still listed in _FREEBSD_TOP_DIRS, so any fix that filters through that
# tuple would silently whitelist it. Files under it ARE caught.
#
# KNOWN MISS, deliberate: bare `gnu` alone is NOT flagged, because sys/gnu
# exists and the lenient kernel-relative exemption resolves it there.
# Distinguishing the two by the writer's trailing slash was tried and
# reverted — chapters write `kern/`, `vm/`, `amd64/` the same way, so it
# cost ~20 false positives to gain this one. See _verify_file_paths.
check("gnu/Makefile flagged", "gnu/Makefile" in flagged("See `gnu/Makefile`."))
check("gnu/COPYING flagged", "gnu/COPYING" in flagged("See `gnu/COPYING`."))
check("librescue flagged", "librescue" in flagged("The `librescue/` dir."))
check("fabricated nested dir flagged",
      "src/tests/bin/cp" in flagged("Tests in `src/tests/bin/cp` cover cp."))

print()

# --- 3) legitimate shapes are NOT flagged ---------------------------------
print("3) no false positives on correct prose")

check("absolute install paths exempt",
      not flagged("Installed to `/boot/kernel` and `/usr/bin`."),
      "installed-system locations are absent from a source checkout by design")

check("src/-relative paths exempt",
      not flagged("Source lives in `src/bin/cp`."),
      "src/bin/cp == <src>/bin/cp")

check("C include paths exempt",
      not flagged("Include `sys/proc.h`, `vm/uma.h`, and `netinet/ip_fw.h`."),
      "<sys/proc.h> is really sys/sys/proc.h")

check("bare kernel dirs exempt",
      not flagged("The `kern` and `vm` directories."),
      "kern == sys/kern")

# The regression that motivates the lenient exemption: ch1 writes kernel
# subdirectories WITH trailing slashes (`kern/`, `vm/`, `amd64/`). An
# earlier attempt treated a trailing slash as a top-level claim and took
# ch1 from 9 flags to 29, nearly all false. Pin the shape here.
check("slash-suffixed kernel dirs exempt",
      not flagged("See `kern/`, `vm/`, `amd64/`, and `arm64/`."),
      "these resolve under sys/ despite the trailing slash")

check("machine/ arch alias exempt",
      not flagged("Include `machine/bus.h` for bus_space."),
      "machine/ points at sys/<arch>/include/ at build time")

check("real paths not flagged",
      not flagged("See `sys/kern/kern_synch.c` and `share/mk/`."))

print()

# --- 4) corrections must be plausible -------------------------------------
print("4) glob corrections stay in the right neighbourhood")

# Basename-only matching produced `sys/conf/config` → `sys/contrib/openzfs/
# config`, sending the writer to an unrelated file. A wrong correction is
# worse than none because the writer trusts it.
out = mod._verify_file_paths(["sys/conf/config"], SRC)
bad_corr = [m for m in out if "→" in m and "openzfs" in m]
check("no cross-subsystem correction", not bad_corr, str(out))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("all path fact-check tests passed")
