#!/usr/bin/env python3
"""Tests for the graph-backed sysctl OID fact-check.

Sysctl OID paths (`vm.pmap.pde.mappings`) are the one symbol class grep
cannot verify: the dotted string is assembled at compile time from a chain
of SYSCTL_* macros and appears nowhere in the source as a literal. This
check verifies claimed OIDs against the codebase-memory-mcp `Sysctl` graph
nodes instead — see the "Sysctl OID paths" banner in generate-doc.py and
CODE_MAP.md "Optional sysctl fact-check".

Two test tiers:
  1. Extractor tests — pure regex, no external deps, always run.
  2. Graph-verify tests — require the codebase-memory-mcp binary AND a ready
     index for FREEBSD_SRC. Auto-SKIPPED (not failed) when the graph is
     unavailable, mirroring the pipeline's graceful-degradation contract.

Run on bigone (the host with ~/freebsd-src and the index):
    python3 test_sysctl_factcheck.py

Exits non-zero on any real test failure; skips are not failures.
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
skips = []


def check(label, cond, info=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if info:
        print(f"         {info}")
    if not cond:
        failures.append(label)


def skip(label, why):
    print(f"  [SKIP] {label}")
    print(f"         {why}")
    skips.append(label)


# --- Tier 1: extractor (no external deps) ----------------------------------
print("Extractor:")

DRAFT = r"""
Tune `kern.ipc.maxsockbuf` and inspect `vm.pmap.pde.mappings`.
Enable routing with `net.inet.ip.forwarding`; also `hw.ncpu` matters.
Compat knob: `compat.linux32.maxvmem`.
NOT sysctls: a method `obj.method`, a file `sys/kern/uipc_socket.c`,
a bare root `kern`, and a non-canonical `mydriver.foo.bar`.
A fabricated tunable `net.inet.tcp.fictional_knob`.
"""

oids = set(mod._extract_sysctls(DRAFT))
check(
    "extracts canonical multi-segment OIDs",
    {"kern.ipc.maxsockbuf", "vm.pmap.pde.mappings", "net.inet.ip.forwarding",
     "hw.ncpu", "compat.linux32.maxvmem",
     "net.inet.tcp.fictional_knob"} <= oids,
    f"got={sorted(oids)}",
)
check(
    "excludes method calls, file paths, bare roots, non-canonical roots",
    not ({"obj.method", "kern", "mydriver.foo.bar"} & oids)
    and not any(o.endswith(".c") for o in oids),
    f"got={sorted(oids)}",
)

# --- Tier 2: graph verification (needs the backend) ------------------------
print("\nGraph verify:")

if not mod._sysctl_graph_available():
    skip("real sysctls accepted / fabricated flagged",
         "codebase-memory-mcp graph unavailable (this is the documented "
         "no-op path; nothing is flagged when the backend is absent).")
    # Contract check that DOES run without a backend: verifier no-ops.
    check(
        "verifier is a no-op when graph unavailable",
        mod._verify_sysctls_via_graph(["net.inet.tcp.fictional_knob"]) == [],
        "must return [] so a missing backend never blocks the pipeline",
    )
else:
    real = ["kern.ipc.maxsockbuf", "vm.pmap.pde.mappings",
            "net.inet.ip.forwarding"]
    fake = ["net.inet.tcp.fictional_knob", "vm.pmap.pde.bogus_fake"]
    missing = set(mod._verify_sysctls_via_graph(real + fake))
    check(
        "real sysctls are NOT flagged",
        not (set(real) & missing),
        f"wrongly flagged: {sorted(set(real) & missing)}",
    )
    check(
        "fabricated sysctls ARE flagged",
        set(fake) <= missing,
        f"missed: {sorted(set(fake) - missing)}",
    )

    # Full fact_check_draft wiring: the key exists and counts toward total.
    fc = mod.fact_check_draft(
        "Set `net.inet.tcp.fictional_knob` to 1.",
        os.path.expanduser(os.environ.get("FREEBSD_SRC", "~/freebsd-src")),
    )
    check(
        "fact_check_draft exposes sysctls_not_found",
        "sysctls_not_found" in fc,
        f"keys={list(fc.keys())}",
    )
    check(
        "fabricated sysctl reaches total_issues via fact_check_draft",
        "net.inet.tcp.fictional_knob" in fc.get("sysctls_not_found", [])
        and fc["total_issues"] >= 1,
        f"sysctls_not_found={fc.get('sysctls_not_found')} total={fc['total_issues']}",
    )

print()
print("=" * 60)
if failures:
    print(f"FAILED: {len(failures)} test(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"All tests passed.{f' ({len(skips)} skipped)' if skips else ''}")
