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

Three more classes, fixed after the same sweep (2026-08-31):

  4. Function-pointer struct members reported as missing functions
     (`_verify_functions`). `pgo_getpages()` and
     `sv_fetch_syscall_args()` are real kernel entry points with no
     `name(` definition line -- only a member declaration plus
     indirect call sites. Two spellings, both real:
     `int (*sv_fetch_syscall_args)(struct thread *);` and the
     typedef'd `pgo_getpages_t\t*pgo_getpages;`. 6 findings.

  5. Driver-local option aliases reported as missing kernel options
     (`_verify_kernel_options`). `#define KTR_CXGBE KTR_SPARE3`
     makes the name real and usable, but only `KTR_SPARE3` reaches
     sys/conf/options. Fixed by asking whether sys/ #defines the
     name before reporting it missing -- which also reclassified
     `KTR_PROC` and `KTR_RUNQ`, two real KTR classes the sweep had
     recorded as genuine defects.

  6. Dotted makefile names read as sysctl OIDs
     (`_extract_sysctls`). `kern` is a real sysctl root and
     `.pre.mk` is two valid segments, so `sys/conf/kern.pre.mk`
     parsed as an OID. 2 findings, both against correct ch4 prose.

  7. `_KERNEL_OPTION_RE` claimed every KTR token as a kernel-config
     option. In the shipped corpus only 4 of 13 were options; the
     rest were trace-class #defines, a driver alias, and a macro
     function. Fixed by dropping the blanket `KTR[A-Z0-9_]*` rule
     from the context-free branch, which required lowering the
     contextual branch's length floor from 5 to 2 -- otherwise
     `options KTR` (3 chars) would have lost its only claimant.
     Corpus candidates 20 -> 8, and the only two now reported are
     the two real hallucinations.

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

# ---------------------------------------------------------------- 6
print("\n6) function-pointer struct members are real functions")
# Each is a kmethod a chapter is right to name as `foo()`. None has a
# `foo(` definition line, so the two-alternative shape grep never saw
# them. The comment records which of the two declaration spellings each
# uses, because they are matched by different alternatives.
FNPTR_MEMBERS = [
    "pgo_getpages",           # typedef'd:  pgo_getpages_t <TAB>*pgo_getpages;
    "pgo_putpages",           # typedef'd
    "sv_fetch_syscall_args",  # inline:     int (*sv_fetch_syscall_args)(...)
    "ift_txd_encap",          # inline
    "ift_rxd_pkt_get",        # inline
    "ift_rxd_refill",         # inline
]
if not HAVE_SRC:
    print("     SKIP (no ~/freebsd-src)")
else:
    mod._FACT_CHECK_CACHE.clear()
    missing = mod._verify_functions(list(FNPTR_MEMBERS), SRC)
    check("all function-pointer members verify",
          missing == [],
          f"reported missing: {missing}" if missing
          else f"all {len(FNPTR_MEMBERS)} verified")

    # The typedef alternative needs a LITERAL TAB: the real line is
    # `pgo_getpages_t\t\t*pgo_getpages;` and BSD `grep -E` does not read
    # `\t` in a bracket expression. Written with only spaces, the two
    # pgo_* symbols silently stay missing while the four inline ones
    # pass -- which is exactly how this first shipped.
    captured = {}
    _real_f = mod._verify_with_cache

    def _spy_f(kind, symbols, src_root, pattern_template, shape_grep, **kw):
        captured["shape_grep"] = shape_grep
        return _real_f(kind, symbols, src_root, pattern_template,
                       shape_grep, **kw)

    mod._verify_with_cache = _spy_f
    try:
        mod._FACT_CHECK_CACHE.clear()
        mod._verify_functions(["pgo_getpages"], SRC)
    finally:
        mod._verify_with_cache = _real_f
    sg = captured.get("shape_grep", "")
    check("func shape grep has both fnptr alternatives",
          "(\\*" in sg and "_t[" in sg,
          f"got {sg!r}")
    check("the typedef alternative carries a literal tab",
          "_t[ \t]" in sg,
          "written with spaces only, the typedef'd pgo_* members stay "
          f"missing; got {sg!r}")

    print("\n6b) fabricated near-misses are still reported missing")
    FN_FAKES = ["pgo_getpagez", "sv_fetch_bogus_args", "ift_txd_nope",
                "zzz_not_real"]
    mod._FACT_CHECK_CACHE.clear()
    missing = mod._verify_functions(list(FN_FAKES), SRC)
    check("every fabricated function still flagged",
          sorted(missing) == sorted(FN_FAKES),
          f"flagged {sorted(missing)}")

    REAL_FUNCS = ["tcp_input", "uma_zalloc", "m_getm2",
                  "bus_alloc_resource", "tcp_newtcpcb"]
    mod._FACT_CHECK_CACHE.clear()
    missing = mod._verify_functions(list(REAL_FUNCS), SRC)
    check("ordinary function definitions still verify",
          missing == [], f"reported missing: {missing}")

# ---------------------------------------------------------------- 7
print("\n7) a #defined option name is not a missing option")
if not HAVE_SRC:
    print("     SKIP (no ~/freebsd-src)")
else:
    # KTR_CXGBE: driver-local alias, absent from sys/conf/options.
    # KTR_PROC / KTR_RUNQ: real classes in sys/sys/ktr_class.h that the
    # sweep's classifier misfiled as genuine defects.
    # KTR_START4: a macro FUNCTION in sys/sys/ktr.h -- excused here
    # because the name is real, though the deeper defect is that
    # _KERNEL_OPTION_RE claims every KTR* token as an option.
    mod._FACT_CHECK_CACHE.clear()
    missing = mod._verify_kernel_options(
        ["KTR_CXGBE", "KTR_PROC", "KTR_RUNQ", "KTR_START4"], SRC)
    check("#defined KTR names are not reported missing",
          missing == [], f"reported missing: {missing}")

    mod._FACT_CHECK_CACHE.clear()
    missing = mod._verify_kernel_options(["INVARIANTS", "WITNESS", "KTR"], SRC)
    check("real config options still verify",
          missing == [], f"reported missing: {missing}")

    print("\n7b) invented options are still reported missing")
    OPT_FAKES = ["VERBOSE_NOPE_XYZ", "DEBUG_FICTIONAL_Q",
                 "INVARIANT_BOGUS_Z"]
    mod._FACT_CHECK_CACHE.clear()
    missing = mod._verify_kernel_options(list(OPT_FAKES), SRC)
    check("every invented option still flagged",
          sorted(missing) == sorted(OPT_FAKES),
          f"flagged {sorted(missing)} -- the #define escape hatch must "
          "not excuse names the tree never defines")

    # The helper must fail CLOSED: a bad root means "not defined", so the
    # option falls through to the corpus check instead of being excused.
    # `getattr` rather than a direct call so a missing helper is one
    # named failure instead of an AttributeError that kills the groups
    # below it -- which is how the pre-fix arm of this file behaved.
    _defined = getattr(mod, "_option_is_defined_in_tree", None)
    check("_option_is_defined_in_tree fails closed on a bad root",
          _defined is not None and _defined("KTR_PROC", "/nonexistent") is False,
          "must exist and return False so the option is still "
          "corpus-checked")

# ---------------------------------------------------------------- 8
print("\n8) a dotted makefile name is not a sysctl OID")
NOT_OIDS = [
    "`kern.pre.mk`",   # sys/conf/kern.pre.mk
    "`kern.post.mk`",  # sys/conf/kern.post.mk
]
for text in NOT_OIDS:
    got = mod._extract_sysctls(text)
    check(f"{text} is not extracted as an OID", got == [], f"got {got}")

print("\n8b) real OIDs are still extracted")
# Includes the three ch40 sysctls that are genuine findings: the fix must
# not stop them reaching the verifier, or a real hallucination ships.
OID_TEXT = ("`kern.ipc.maxsockbuf` `vm.pmap.pg_ps_enabled` "
            "`net.inet.ip.forwarding` `debug.witness.skipspin` "
            "`hw.usb.xhci.route` `hw.usb.xhci.polling`")
got = mod._extract_sysctls(OID_TEXT)
want = sorted(["kern.ipc.maxsockbuf", "vm.pmap.pg_ps_enabled",
               "net.inet.ip.forwarding", "debug.witness.skipspin",
               "hw.usb.xhci.route", "hw.usb.xhci.polling"])
check("all six real OIDs still extracted", got == want,
      f"got {got}")

# A leaf that merely LOOKS like a short word must survive -- the suffix
# list is closed on purpose, and this pins that it stays small.
got = mod._extract_sysctls("`kern.sched.quantum` `vm.stats.vm.v_free_count`")
check("short real leaves are not mistaken for extensions",
      got == ["kern.sched.quantum", "vm.stats.vm.v_free_count"],
      f"got {got}")

# ---------------------------------------------------------------- 9
print("\n9) the option extractor does not over-claim KTR tokens")
# A KTR trace class is not a kernel-config option, and asking the option
# verifier about one is a category error. `_option_is_defined_in_tree`
# suppresses the resulting report, but these must not be CLAIMED at all.
NOT_OPTIONS = [
    "`KTR_PROC`",     # trace class, sys/sys/ktr_class.h
    "`KTR_RUNQ`",     # trace class
    "`KTR_GEOM`",     # trace class
    "`KTR_GEN`",      # trace class
    "`KTR_SPARE3`",   # trace class
    "`KTR_VMM`",      # alias of KTR_GEN
    "`KTR_CXGBE`",    # driver-local alias
    "`KTR_START4`",   # macro FUNCTION, sys/sys/ktr.h
    "`KTR_COMPILE`",  # a real option, but not claimed without context
    "`KTR_ENTRIES`",  # ditto
]
for text in NOT_OPTIONS:
    got = mod._extract_kernel_options(text)
    check(f"bare {text} is not claimed as an option",
          got == [], f"got {got}")

print("\n9b) contextual option claims are still extracted")
# Branch 1 keys on the `option`/`options` keyword, so the draft itself
# has asserted the claim. `KTR` and `DDB` are the regression cases: both
# are real options in sys/conf/options and both are shorter than the old
# 5-char floor, so before the floor was lowered branch 1 could never
# verify them and only branch 2's blanket rule reached `KTR`.
CONTEXTUAL = [
    ("Build with `options VERBOSE_SYSINIT` to trace.", ["VERBOSE_SYSINIT"]),
    ("compile with `options KTR` enabled", ["KTR"]),
    ("`options DDB`", ["DDB"]),
    ("options WITNESS", ["WITNESS"]),
    ("options P1003_1B_MQUEUE", ["P1003_1B_MQUEUE"]),
]
for text, want in CONTEXTUAL:
    got = mod._extract_kernel_options(text)
    check(f"{text[:38]!r} -> {want}", got == want, f"got {got}")

print("\n9c) invented-option shapes are still claimed without context")
# These four prefixes stay in the context-free branch because they have
# no other meaning in kernel prose. VERBOSE_FAILURE and
# VERBOSE_FAILURE_PROGRESS are in the shipped corpus, are neither
# options nor #defines anywhere in sys/, and are the whole reason the
# context-free branch exists.
INVENTED = [
    ("`VERBOSE_FAILURE`", ["VERBOSE_FAILURE"]),
    ("`VERBOSE_FAILURE_PROGRESS`", ["VERBOSE_FAILURE_PROGRESS"]),
    ("`DEBUG_VFS_LOCKS`", ["DEBUG_VFS_LOCKS"]),
    ("`INVARIANTS`", ["INVARIANTS"]),
    ("`WITNESS_SKIPSPIN`", ["WITNESS_SKIPSPIN"]),
]
for text, want in INVENTED:
    got = mod._extract_kernel_options(text)
    check(f"{text} still claimed", got == want, f"got {got}")

if HAVE_SRC:
    print("\n9d) end to end: only the real hallucinations are reported")
    text = ("Enable `options KTR` and set the `KTR_GEOM` trace-class bit. "
            "For cxgbe the `KTR_CXGBE` class is `KTR_SPARE3`. "
            "Build with `options VERBOSE_SYSINIT`, and `VERBOSE_FAILURE` "
            "reports the rest.")
    claimed = mod._extract_kernel_options(text)
    mod._FACT_CHECK_CACHE.clear()
    reported = mod._verify_kernel_options(claimed, SRC)
    check("only VERBOSE_FAILURE is reported missing",
          reported == ["VERBOSE_FAILURE"],
          f"claimed {claimed}, reported {reported}")

# ---------------------------------------------------------------- exit
print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")
