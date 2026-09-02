#!/usr/bin/env python
"""`#define` field aliases are real fields, not hallucinations.

ch8 (process management), 2026-09-02 post-mortem
------------------------------------------------
The fact-check flagged `struct thread->td_retval` as a nonexistent
field. It exists — as a macro alias onto a nested union member:

    sys/sys/proc.h:365
    #define td_retval    td_uretoff.tdu_retval

and is used in ~95 files under sys/. `_parse_struct_fields` only reads
`;`-terminated declarators inside `struct NAME { ... }`, so alias
`#define`s are invisible to it. The writer complied with the "field does
not exist" finding and DELETED correct prose; README_process.md shipped
with no `td_retval` mention and no UNVERIFIED banner.

Same fail-CLOSED shape as ch4's wrong-winner defect: the hallucination
detector manufactures the hallucination, the writer obeys, and every
later stage certifies the damage as clean.

Fix: after picking the winning definition, also harvest
`#define <name> <existing_field>...` aliases from the defining file and
union them into the returned field set. The replacement text must START
with a field the struct really declares — proc.h alone has 326
`#define`s, and accepting all of them would make the field verifier a
rubber stamp.
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


print("1) the ch8 defect: struct thread->td_retval is a real field")
gd._STRUCT_FIELDS_CACHE.clear()
th = gd._real_struct_fields("thread", SRC, None)
check("struct thread resolves", len(th) > 50, f"{len(th)} fields")
check("td_retval present (alias at proc.h:365)", "td_retval" in th)
check("td_siglist present (alias at proc.h:254)", "td_siglist" in th)
check("underlying member td_uretoff still present", "td_uretoff" in th)

print("2) layer 2 (prose member-access) no longer false-flags it")
gd._STRUCT_FIELDS_CACHE.clear()
issues = gd._verify_struct_field_claims(
    [("thread", "td_retval"), ("thread", "td_siglist"),
     ("thread", "td_proc")],
    SRC, None)
check("no issue raised for td_retval/td_siglist/td_proc",
      not issues, f"got {issues}")

print("3) real hallucinations are still caught (no rubber stamp)")
gd._STRUCT_FIELDS_CACHE.clear()
bogus = gd._verify_struct_field_claims(
    [("thread", "td_totally_invented_xyzzy")], SRC, None)
check("invented field still flagged", len(bogus) == 1, f"got {bogus}")

# proc.h has 326 #defines. If the harvester accepted any of them, the
# non-field macros would leak in and neuter the verifier.
gd._STRUCT_FIELDS_CACHE.clear()
th2 = gd._real_struct_fields("thread", SRC, None)
leaks = [n for n in ("TDF_BORROWING", "TD_IS_RUNNING", "PROC_LOCK",
                     "FIRST_THREAD_IN_PROC", "TID_BUFFER_SIZE")
         if n in th2]
check("unrelated proc.h macros did not leak in", not leaks,
      f"leaked {leaks}")

print("4) alias harvesting generalises past proc.h")
# sys/sys/mbuf.h uses the same idiom for the external-page fields:
#   #define m_epg_pa    m_ext.extpg_pa
gd._STRUCT_FIELDS_CACHE.clear()
mb = gd._real_struct_fields("mbuf", SRC, None)
check("struct mbuf resolves", len(mb) > 10, f"{len(mb)} fields")
check("m_epg_pa present (alias onto m_ext)", "m_epg_pa" in mb,
      f"m_ext present={'m_ext' in mb}")

print("5) no regression: ambiguity bail and unknown structs unchanged")
gd._STRUCT_FIELDS_CACHE.clear()
amb = gd._real_struct_fields(
    "device", SRC, ["share/mk", "tools/build", "usr.sbin/config"])
check("ambiguous struct device still returns empty", not amb,
      f"got {len(amb)} fields")

gd._STRUCT_FIELDS_CACHE.clear()
none = gd._real_struct_fields("definitely_not_a_struct_xyzzy", SRC, None)
check("unknown struct still empty", not none)

if FAILS:
    print(f"\n{len(FAILS)} FAILURE(S): {', '.join(FAILS)}")
    sys.exit(1)
print("\nAll checks passed.")
