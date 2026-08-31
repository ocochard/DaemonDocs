#!/usr/bin/env python3
"""Tests for three verifier defects that fabricated fact-check findings.

Every check here regresses a *false positive*: a claim the writer got
RIGHT that the verifier reported as wrong. That direction matters more
than it looks. A fabricated finding is handed to the reviewer, which
correctly fails the chapter's Accuracy criterion over it, and then to
the writer as a fact-fix instruction -- so the writer is told to delete
or "correct" accurate prose. ch37 (TCP) was instructed to remove a real
named struct and flatten it into an anonymous one; ch34 and ch38 were
graded FAIL with no other symbol finding at all.

Measured by running `fact_check_draft` over the 21 shipped chapters
that carry an UNVERIFIED banner (2026-08-31): 20 false findings across
three defects; chapters with zero genuine symbol findings went 7 -> 10.

  1. queue(3) macro-declared structs (`_verify_structs`).
     `TAILQ_HEAD(pglist, vm_page);` IS the definition of
     `struct pglist` -- no `struct pglist {` line exists anywhere in
     the tree. 8 occurrences: pglist, rq_queue, buflists,
     sysctl_ctx_list, db_command_table, ip6fraghead, ip6qhead,
     note_info_list.

  2. English sentence boundary read as a member access
     (`_extract_struct_field_claims`, prose stage). NOT an arrow
     problem: the loose regex allows `\\s*` around the operator and
     Python's `\\s` matches newlines, so "invoked with the bio. GEOM
     is responsible" parsed as `bio` . `GEOM`. 8 occurrences, every
     one a sentence boundary and not one a real member access.

  3. Slash-separated identifier shorthand read as a file path
     (`_extract_file_paths`). `bus_space_read_1/2/4/8` is four
     functions, `nm_acregmin/max` two fields, `nbuf/8` arithmetic.
     4 occurrences.

Each group also pins the opposite direction. A verifier that stops
reporting false positives by going blind is strictly worse than the
bug: a false negative ships a hallucination stamped as verified.

Run on the host with ~/freebsd-src:
    python3 test_verifier_false_positives.py
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


# ---------------------------------------------------------------- 1
print("1) queue(3) macro-declared structs verify")
# These have NO `struct NAME {` line in the tree. Each was cited
# correctly by a shipped chapter and reported missing. The comment
# names the declaring macro so a future reader can re-check the claim
# without re-deriving it.
MACRO_STRUCTS = [
    "pglist",            # sys/vm/vm_page.h      TAILQ_HEAD(pglist, vm_page)
    "rq_queue",          # sys/sys/runq.h        TAILQ_HEAD(rq_queue, thread)
    "buflists",          # sys/sys/bufobj.h      TAILQ_HEAD(buflists, buf)
    "sysctl_ctx_list",   # sys/sys/sysctl.h      TAILQ_HEAD(...)
    "db_command_table",  # sys/ddb/ddb.h         LIST_HEAD(...)
    "ip6fraghead",       # sys/netinet6/ip6_var.h
    "ip6qhead",          # sys/netinet6/frag6.c
    "note_info_list",    # sys/kern/imgact_elf.c
]
if not HAVE_SRC:
    print("     SKIP (no ~/freebsd-src)")
else:
    missing = verify(MACRO_STRUCTS)
    check("all queue(3)-declared structs verify",
          missing == [],
          f"reported missing: {missing}" if missing
          else f"all {len(MACRO_STRUCTS)} verified")

    # The stage-2/stage-3 split is the trap: whatever the grep filter
    # drops, the Python re-scan never sees. Both layers must know the
    # macro shape, so capture what actually reaches each one.
    captured = {}
    _real = mod._verify_with_cache

    def _spy(kind, symbols, src_root, pattern_template, shape_grep, **kw):
        captured["shape_grep"] = shape_grep
        captured["pattern_template"] = pattern_template
        return _real(kind, symbols, src_root, pattern_template,
                     shape_grep, **kw)

    mod._verify_with_cache = _spy
    try:
        mod._FACT_CHECK_CACHE.clear()
        mod._verify_structs(["pglist"], SRC)
    finally:
        mod._verify_with_cache = _real

    sg = captured.get("shape_grep", "")
    pt = captured.get("pattern_template", "")
    check("stage-2 grep filter knows the queue(3) macros",
          "TAILQ" in sg and "HEAD" in sg,
          f"got {sg!r}")
    # BSD `grep -E` does NOT interpret `\t` inside a bracket expression:
    # `[ \t]` matches space, backslash, or the letter t, so a tab-indented
    # definition falls out of stage 2 and the symbol reports missing. The
    # pattern must carry a literal tab byte. This is invisible to any test
    # that goes through Python's `re`, which is why it is asserted on the
    # captured pattern instead.
    check("stage-2 pattern carries literal tabs, not the two-char \\t",
          "\t" in sg and "\\t" not in sg,
          f"got {sg!r}")
    check("stage-3 re-scan pattern knows them too",
          "TAILQ" in pt,
          "stage 3 must match or the symbol is dropped after stage 2 "
          f"passes it; got {pt!r}")
    check("stage-2 macro alternative is not ^-anchored",
          "^(TAILQ" not in sg and "|^(TAILQ" not in sg,
          "these appear indented inside enclosing struct bodies")

# ---------------------------------------------------------------- 2
print("\n2) fabricated structs are STILL reported missing")
# The guard on group 1. Near-misses built from the real names are the
# interesting cases: a fix that merely matched any token adjacent to a
# queue macro would pass group 1 and fail here.
if not HAVE_SRC:
    print("     SKIP (no ~/freebsd-src)")
else:
    FAKES = [
        "pglist_bogus",       # near-miss on a real macro struct
        "rq_queue_fake",      # near-miss on a real macro struct
        "tcp_stat",           # ch37's genuine hallucination (real: tcpstat)
        "nonexistent_xyzzy",
        "vm_page_bogus",
    ]
    missing = verify(FAKES)
    check("every fabricated struct still flagged",
          sorted(missing) == sorted(FAKES),
          f"flagged {sorted(missing)}, expected {sorted(FAKES)}")

    # The three spellings fixed in c6f6635 must not regress: plain,
    # tab-separated, nested/indented, and typedef.
    REAL_SPELLINGS = [
        "vm_page",       # plain `struct vm_page {`
        "arphdr",        # tab-separated `struct<TAB>arphdr {`
        "in_endpoints",  # nested inside struct in_conninfo
        "ksiginfo",      # typedef struct ksiginfo {
        "inpcb",
    ]
    missing = verify(REAL_SPELLINGS)
    check("the four non-macro spellings still verify",
          missing == [],
          f"reported missing: {missing}")

# ---------------------------------------------------------------- 3
print("\n3) an English sentence boundary is not a field claim")
# Real text from the shipped chapters, not invented. The period plus
# space plus capitalized word is what the loose regex read as
# `var . field`. The `socket.\n\nFor` case spans a blank line, which is
# why `\s` mattering across newlines is load-bearing here.
PROSE = (
    "The strategy routine installed by da(4) is invoked with the bio. "
    "GEOM is responsible for the rest.\n"
    "`dastrategy()` in `sys/cam/scsi/scsi_da.c` receives the bio. "
    "It decides the operation.\n"
    "The same CCB can address a 137 GB disk or a multi-terabyte disk. "
    "The NVMe command differs.\n"
    "the five-tuple is hashed to the inpcb, and the inpcb points at "
    "the socket.\n\nFor TCP, more state follows.\n"
    "which hangs off the inpcb. This split is the reason.\n"
    "and a pointer to a MAC label. Because a credential is shared.\n"
    "the dnode lives in the objset. The dsl layer owns it.\n"
)
KNOWN = ["bio", "disk", "socket", "inpcb", "label", "objset"]
claims = mod._extract_struct_field_claims(PROSE, known_structs=KNOWN)
check("no sentence boundary becomes a field claim",
      claims == [],
      f"got {claims}")

print("\n3b) real member-access claims are still extracted")
# The ch2 (Boot Process) failure this extractor was written for. If
# this regresses, the whole prose stage is dead weight.
ch2 = "The writer claims `bootinfo->bi_efi_memmap` holds the map.\n"
claims = mod._extract_struct_field_claims(ch2, known_structs=["bootinfo"])
check("prose `NAME->FIELD` still caught (the ch2 case)",
      claims == [("bootinfo", "bi_efi_memmap")],
      f"got {claims}")

# Fenced C keeps the LOOSE regex on purpose: real code may wrap a long
# `->` chain across lines, and inside a fence there is no English to
# confuse it with.
fenced = "```c\nstruct bio *bp;\nbp->bio_cmd = BIO_READ;\n```\n"
claims = mod._extract_struct_field_claims(fenced, known_structs=["bio"])
check("fenced C member access still caught",
      claims == [("bio", "bio_cmd")],
      f"got {claims}")

fenced_wrapped = "```c\nstruct bio *bp;\nbp->\n    bio_cmd = 1;\n```\n"
claims = mod._extract_struct_field_claims(
    fenced_wrapped, known_structs=["bio"])
check("fenced C tolerates a wrapped -> chain",
      claims == [("bio", "bio_cmd")],
      f"got {claims} (fences must keep the loose regex)")

# ---------------------------------------------------------------- 4
print("\n4) slash-separated identifier shorthand is not a path")
SHORTHAND = [
    "`nm_acregmin/max`",         # ch17: two struct fields
    "`nm_acdirmin/max`",         # ch17
    "`bus_space_read_1/2/4/8`",  # ch23: four functions
    "`bus_space_write_1/2/4/8`",  # ch23
    "`nbuf/8`",                  # ch11: arithmetic
]
for text in SHORTHAND:
    got = mod._extract_file_paths(text)
    check(f"{text} is not extracted as a path", got == [], f"got {got}")

print("\n4b) real path claims are still extracted")
# `gnu/` is the load-bearing one: it was RETIRED upstream
# (134a4c78d070), so the whole point is that it stays extractable and
# gets refuted by verification. A filter that dropped it would
# whitelist exactly the hallucination the directory branch exists to
# catch.
REAL_PATHS = [
    ("`sys/vm/vm_page.c`", "sys/vm/vm_page.c"),
    ("`stand/efi/loader/bootinfo.c`", "stand/efi/loader/bootinfo.c"),
    ("`share/mk/bsd.own.mk`", "share/mk/bsd.own.mk"),
    ("`sys/conf/kern.pre.mk`", "sys/conf/kern.pre.mk"),
    ("`sys/fs/nfsclient/`", "sys/fs/nfsclient"),
    ("`gnu/`", "gnu"),
]
for text, want in REAL_PATHS:
    got = mod._extract_file_paths(text)
    check(f"{text} still extracted", got == [want], f"got {got}")

# A numeric LEAF is a manual-page path, not the `nbuf/8` shape -- the
# rejection must key on a numeric segment mid-path, not any digit.
got = mod._extract_file_paths("`share/man/man9/malloc.9`")
check("a man-page path with a numeric extension survives",
      got == ["share/man/man9/malloc.9"], f"got {got}")

# ---------------------------------------------------------------- 5
print("\n5) no FreeBSD top-level directory contains an underscore")
# The group-4 rule rejects an underscored first segment. That is only
# safe while no real top-level directory has one -- if FreeBSD ever
# adds one, this test fails and names the rule that must change.
if not HAVE_SRC:
    print("     SKIP (no ~/freebsd-src)")
else:
    tops = [d for d in os.listdir(SRC)
            if os.path.isdir(os.path.join(SRC, d)) and not d.startswith(".")]
    offenders = [d for d in tops if "_" in d]
    check("no top-level dir has an underscore",
          offenders == [],
          f"found {offenders} -- the underscore rule in "
          "_extract_file_paths would now suppress real path claims"
          if offenders else f"checked {len(tops)} top-level dirs")

# ---------------------------------------------------------------- exit
print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")
