#!/usr/bin/env python
"""Ambiguous struct names must not produce an "authoritative" field list.

ch4 (build system), 2026-09-01 post-mortem
------------------------------------------
The tree holds six `struct device` definitions. `_real_struct_fields`
selected by MAX FIELD COUNT, so the 22-field linuxkpi struct
(sys/compat/linuxkpi/common/include/linux/device.h) beat config(8)'s
4-field one (usr.sbin/config/config.h) — the one the chapter was
actually documenting.

The fact-check then told the writer its verbatim-correct fields
(d_done, d_name, yyfile, d_next) "do not exist", supplied the linuxkpi
field list as authoritative, and instructed "Do NOT re-derive them by
reading the header." The writer refused and was right. Had it complied,
correct prose would have been rewritten into wrong prose that every
downstream stage would then certify as clean — a fail-CLOSED defect,
unlike ch21's fail-open stub.

Fix: when candidate definitions disagree, return empty
("verification unavailable"), which callers already treat as don't-flag.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.expanduser("~/freebsd-src")

spec = importlib.util.spec_from_file_location(
    "gd", os.path.join(REPO, "generate-doc.py"))
gd = importlib.util.module_from_spec(spec)
sys.modules["gd"] = gd
spec.loader.exec_module(gd)

FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")
    if not ok:
        FAILS.append(label)


print("1) ambiguous struct name yields no authoritative field list")
gd._STRUCT_FIELDS_CACHE.clear()
amb = gd._real_struct_fields(
    "device", SRC, ["share/mk", "tools/build", "usr.sbin/config"])
check("struct device returns empty (ambiguous)", not amb,
      f"got {len(amb)} fields")
check("linuxkpi fields not asserted", "bsddev" not in amb)
check("config(8) fields not asserted either", "d_done" not in amb)

print("2) unambiguous structs still verify (no regression)")
gd._STRUCT_FIELDS_CACHE.clear()
uniq = gd._real_struct_fields(
    "cfgfile", SRC, ["share/mk", "tools/build", "usr.sbin/config"])
check("struct cfgfile resolves", bool(uniq), f"fields={sorted(uniq)}")
check("cfg_path present", "cfg_path" in uniq)

gd._STRUCT_FIELDS_CACHE.clear()
vm = gd._real_struct_fields("vm_page", SRC, None)
check("struct vm_page resolves", len(vm) >= 10, f"{len(vm)} fields")
check("vm_page has real field 'busy_lock'", "busy_lock" in vm)

print("3) empty result means don't-flag, not all-fields-bogus")
bogus, abridged = gd._verify_struct_bodies(
    [("device", ["d_done", "d_name", "yyfile", "d_next"],
      "int d_done; char *d_name; char *yyfile;")],
    SRC, ["share/mk", "tools/build", "usr.sbin/config"])
check("no bogus-field issue raised for ambiguous struct",
      not bogus, f"got {bogus}")
check("no zero-overlap issue raised either",
      not abridged, f"got {abridged}")

print("4) burial: ubiquitous struct names still resolve")
# ch8, 2026-09-02: `struct thread` matched 1012 files under sys/; the
# real sys/sys/proc.h sorted to rank 39 and candidates[:32] never
# opened it, so it silently returned "verification unavailable".
gd._STRUCT_FIELDS_CACHE.clear()
th = gd._real_struct_fields("thread", SRC, None)
check("struct thread resolves despite 1000+ mentions", len(th) > 50,
      f"{len(th)} fields")
check("thread has real field 'td_proc'", "td_proc" in th)

gd._STRUCT_FIELDS_CACHE.clear()
pr = gd._real_struct_fields("proc", SRC, None)
check("struct proc resolves", len(pr) > 50, f"{len(pr)} fields")

print("5) test stubs are not competing definitions")
# sys/netpfil/ipfw/test/dn_test.h defines a 2-field "fake mbuf". The
# definition-shape prefilter promotes it into the candidate slice, so
# without a test-path exclusion it would make the real sys/sys/mbuf.h
# ambiguous and unverifiable.
gd._STRUCT_FIELDS_CACHE.clear()
mb = gd._real_struct_fields("mbuf", SRC, None)
check("struct mbuf resolves (test stub ignored)", len(mb) > 10,
      f"{len(mb)} fields")
check("mbuf has real field 'm_next'", "m_next" in mb)

print("6) nonexistent struct stays empty (not a crash)")
gd._STRUCT_FIELDS_CACHE.clear()
none = gd._real_struct_fields("definitely_not_a_struct_xyzzy", SRC, None)
check("unknown struct returns empty", not none)

if FAILS:
    print(f"\n{len(FAILS)} FAILURE(S): {', '.join(FAILS)}")
    sys.exit(1)
print("\nAll checks passed.")
