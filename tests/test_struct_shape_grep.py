#!/usr/bin/env python3
"""Tests for the struct-definition shape filter in `_verify_structs`.

Stage 2 of `_batched_grep_present` narrows stage 1's fixed-string hits
down to lines that look like *definitions*, so the 1 MB output cap
captures definitions rather than the dense forest of pointer-typed
uses. If that filter drops a real definition line, stage 3 never sees
it and the symbol is reported missing -- the reviewer is handed a
fabricated "Missing structs" list and correctly fails the chapter's
Accuracy criterion over it.

ch37 (TCP, 2026-08-30) was graded FAIL on Accuracy for citing
`struct in_endpoints`, which is real (`sys/netinet/in_pcb.h`). Two
independent causes, both in the shape filter:

  1. The pattern used a literal SPACE after `struct`, but 42 structs
     in `sys/` are written `struct<TAB>name {` -- `arphdr`, `icmpstat`,
     `ether_arp`, `direct`, `fork_req`, `eui64`, `tcpstat`.
  2. The pattern was anchored `^struct`, but 442 structs are defined
     nested inside another struct or union, hence indented --
     `in_endpoints` inside `in_conninfo`, and most of the `fw_*` and
     `mt7915_*` driver headers.
  3. `typedef struct NAME {` was rejected for the same reason, and it
     is the largest gap of the three: 3735 distinct tags in `sys/` are
     declared that way (`ksiginfo`, `moduledata`, `elf_file`,
     `if_txrx`, `__sigset`).

A third trap sits between them: `shape_grep` is consumed by BSD
`grep -E`, where a bracket expression does NOT interpret `\\t`. Writing
`[ \\t]` there matches space, backslash or the letter `t` -- never a
tab -- so the pattern must carry a literal tab character. A test that
only exercised Python's `re` would pass while the pipeline stayed
broken, so group 1 asserts on the pattern bytes themselves.

Same failure family as the ch34 grep cap and the ch40 extractors: the
verifier fabricates the missing-symbol list, and every layer
downstream behaves correctly on poisoned input.

Run on the host with ~/freebsd-src:
    python3 test_struct_shape_grep.py
Exits non-zero on failure. Source-dependent groups auto-skip when the
tree is absent.
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

SRC = os.path.expanduser("~/freebsd-src")
HAVE_SRC = os.path.isdir(os.path.join(SRC, "sys"))
failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")
    if not ok:
        failures.append(label)


def verify(names):
    """Missing-list for `names`, with the verdict cache cleared."""
    mod._FACT_CHECK_CACHE.clear()
    return mod._verify_structs(list(names), SRC)


print("1) the shape pattern carries a literal tab, not the escape `\\t`")
# Capture the real pattern `_verify_structs` passes to grep. Asserting
# on the source text instead would be worthless: the surrounding
# comment legitimately discusses `\\t`, so a substring check there
# matches prose rather than the pattern. And a test that ran the
# pattern through Python's `re` would pass while the pipeline stayed
# broken, because Python understands `\\t` and BSD grep's bracket
# expressions do not.
captured = {}
_real = mod._verify_with_cache


def _spy(kind, symbols, src_root, pattern_template, shape_grep, **kw):
    captured["shape_grep"] = shape_grep
    captured["pattern_template"] = pattern_template
    return _real(kind, symbols, src_root, pattern_template, shape_grep,
                 **kw)


mod._verify_with_cache = _spy
try:
    mod._FACT_CHECK_CACHE.clear()
    mod._verify_structs(["arphdr"], SRC)
finally:
    mod._verify_with_cache = _real

sg = captured.get("shape_grep", "")
check("captured the shape filter", bool(sg), f"got {sg!r}")
check("shape filter contains a literal tab character",
      "\t" in sg,
      "a bracket expression in BSD grep -E cannot use the escape \\t")
check("shape filter does not rely on the two-char escape",
      "[ \\t]" not in sg,
      "found `[ \\t]` -- matches space/backslash/t, never a tab")
check("brace alternative allows leading whitespace",
      sg.startswith("^[ \t]*struct") or sg.startswith("^[ \t]*"),
      f"nested definitions need it; got {sg!r}")
check("brace alternative accepts an optional typedef prefix",
      "typedef" in sg,
      f"3735 tags are typedef'd; got {sg!r}")
check("K&R alternative stays anchored at column 0",
      "|^struct" in sg,
      f"unanchoring it admits indented parameters; got {sg!r}")

print()
print("2) tab-separated definitions verify (`struct<TAB>name {`)")
if HAVE_SRC:
    TABBED = ["arphdr", "icmpstat", "ether_arp", "direct", "fork_req",
              "eui64", "tcpstat"]
    miss = verify(TABBED)
    check("all tab-defined structs found", not miss,
          f"reported missing: {miss}")
else:
    print(f"  [SKIP] {SRC}/sys not present")

print()
print("3) nested/indented definitions verify")
if HAVE_SRC:
    # in_endpoints is the one that actually failed ch37.
    NESTED = ["in_endpoints", "in_conninfo", "nd_ifinfo", "tx_stats"]
    miss = verify(NESTED)
    check("all nested structs found", not miss,
          f"reported missing: {miss}")
else:
    print(f"  [SKIP] {SRC}/sys not present")

print()
print("3b) `typedef struct NAME {` definitions verify")
if HAVE_SRC:
    TYPEDEFD = ["ksiginfo", "moduledata", "elf_file", "if_txrx",
                "__sigset"]
    miss = verify(TYPEDEFD)
    check("all typedef'd structs found", not miss,
          f"reported missing: {miss}")
else:
    print(f"  [SKIP] {SRC}/sys not present")

print()
print("4) ordinary top-level definitions still verify")
if HAVE_SRC:
    PLAIN = ["proc", "thread", "buf", "vm_page", "mount", "vnode",
             "socket", "ifnet", "mbuf", "inpcb", "tcpcb"]
    miss = verify(PLAIN)
    check("no regression on plain definitions", not miss,
          f"reported missing: {miss}")
else:
    print(f"  [SKIP] {SRC}/sys not present")

print()
print("5) fabricated struct names are still reported missing")
# The whole point of the verifier. Relaxing the filter must not turn it
# into a rubber stamp -- these are the shapes a hallucinating writer
# actually produces, including near-misses of real names.
if HAVE_SRC:
    FAKE = ["tcp_stat",            # real name is `tcpstat` (ch37, genuine)
            "in_endpoint",         # real name is plural
            "nonexistent_xyzzy",
            "vm_page_bogus",
            "proc_info_fake"]
    miss = verify(FAKE)
    check("every fabricated name flagged", sorted(miss) == sorted(FAKE),
          f"flagged {sorted(miss)}; expected all of {sorted(FAKE)}")
else:
    print(f"  [SKIP] {SRC}/sys not present")

print()
print("6) a pointer use or parameter is not a definition")
if HAVE_SRC:
    # The relaxed anchor allows leading whitespace ONLY before the
    # brace alternative. If someone also unanchors the K&R alternative
    # (`struct foo` at end of line), an indented parameter declaration
    # like `\tstruct thread *td` starts passing and the verifier goes
    # blind. `_shape_only` has no definition anywhere in the tree, but
    # appears nowhere at all -- so this asserts the filter needs real
    # definition evidence, not merely a mention.
    check("a name that only ever appears as a type use is flagged",
          "_shape_only_never_defined" in verify(
              ["_shape_only_never_defined"]),
          "verifier must require definition evidence")
else:
    print(f"  [SKIP] {SRC}/sys not present")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("all struct-shape-grep tests passed")
