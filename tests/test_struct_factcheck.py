#!/usr/bin/env python3
"""Smoke test for the struct-body fact-check pass.

Run on framework: `python3 test_struct_factcheck.py`.
Imports generate-doc.py via importlib (the hyphen in the filename
prevents normal `import`). Exits non-zero on any test failure.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)  # tests/ lives one level below the repo root
spec = importlib.util.spec_from_file_location(
    "gendoc", os.path.join(REPO, "generate-doc.py")
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


# 1) Round-trip the real struct sysinit body.
print("Test 1: parse real struct sysinit body")
real_body = """
        enum sysinit_sub_id     subsystem;
        enum sysinit_elem_order order;
        STAILQ_ENTRY(sysinit)   next;
        sysinit_cfunc_t func;
        const void      *udata;
"""
parsed = mod._parse_struct_fields(real_body)
expected = {"subsystem", "order", "next", "func", "udata"}
check(
    "parser yields the real 5 fields",
    set(parsed) == expected,
    f"got={parsed}",
)
print()

# 2) Bad draft from framework's actual sys/README.md (current state).
print("Test 2: hallucinated struct sysinit (matches framework's sys/README.md)")
bad_draft = """
Some prose.

```c
struct sysinit {
    void (*func) (void *);      /* Function to call */
    void *data;                 /* Data pointer */
    int si_sub;                 /* Subsystem order */
    int si_order;               /* Order within subsystem */
    const char *name;           /* Name */
};
```
"""
claims = mod._extract_struct_bodies(bad_draft)
check(
    "extracted exactly 1 struct claim",
    len(claims) == 1 and claims[0][0] == "sysinit",
    f"claims={claims}",
)
verify_result = mod._verify_struct_bodies(claims, SRC)
# `_verify_struct_bodies` now returns (bogus_field_issues, abridged_issues).
# Older tests assumed a flat list; unpack the tuple here.
if isinstance(verify_result, tuple):
    bogus_issues, _abridged_issues = verify_result
else:
    bogus_issues = verify_result
check(
    "bogus fields flagged",
    len(bogus_issues) == 1 and "sysinit" in bogus_issues[0],
    f"issues={bogus_issues}",
)
# The claim's `func` exists; `data`, `si_sub`, `si_order`, `name` do not.
# The exact bogus list depends on parser behavior on `void (*func)(void*)`
# — that line's last token is `void` after our paren-stripper runs, so
# the parser may pull `void` (rejected by the identifier guard? no, it
# matches `[A-Za-z_]\w*`) or skip it. Just check the *clearly bogus*
# names get flagged.
issue_text = " ".join(bogus_issues)
for bogus in ("data", "si_sub", "si_order", "name"):
    check(f"  bogus field flagged: {bogus}", bogus in issue_text)
print()

# 3) Verbatim-correct claim should pass clean.
print("Test 3: verbatim-correct struct sysinit claim")
ok_draft = """
```c
struct sysinit {
    enum sysinit_sub_id subsystem;
    enum sysinit_elem_order order;
    STAILQ_ENTRY(sysinit) next;
    sysinit_cfunc_t func;
    const void *udata;
};
```
"""
ok_claims = mod._extract_struct_bodies(ok_draft)
ok_result = mod._verify_struct_bodies(ok_claims, SRC)
if isinstance(ok_result, tuple):
    ok_bogus, ok_abridged = ok_result
else:
    ok_bogus, ok_abridged = ok_result, []
check(
    "verbatim-correct claim raises 0 bogus-field issues",
    ok_bogus == [],
    f"bogus={ok_bogus}",
)
check(
    "verbatim-correct claim raises 0 abridged issues",
    ok_abridged == [],
    f"abridged={ok_abridged}",
)
print()

# 4) Inline prose (not in a code block) must not extract.
print("Test 4: inline prose mention is NOT flagged")
prose = "A struct sysinit { foo; bar; baz; } shown inline in prose."
check(
    "no claims extracted from prose",
    mod._extract_struct_bodies(prose) == [],
)
print()

# 5) Elided body (`/* ... */` placeholder) must not extract.
print("Test 5: elided body is NOT flagged")
elided = """
```c
struct sysinit { /* ... */ };
```
"""
check(
    "no claims extracted from elided body",
    mod._extract_struct_bodies(elided) == [],
)
print()

# 6) End-to-end fact_check_draft — total_issues includes our new count.
print("Test 6: fact_check_draft surfaces struct_fields_bogus")
res = mod.fact_check_draft(bad_draft, SRC)
check(
    "result has struct_fields_bogus key",
    "struct_fields_bogus" in res,
)
check(
    "struct_fields_bogus is non-empty for bad draft",
    bool(res.get("struct_fields_bogus")),
    f"value={res.get('struct_fields_bogus')}",
)
check(
    "total_issues includes struct field count",
    res["total_issues"] >= len(res["struct_fields_bogus"]),
)
print()

# 7) Tricky: struct proc with macro-bodied fields and an injected bogus one.
print("Test 7: struct proc with macros + 1 injected bogus field")
proc_draft = """
```c
struct proc {
    LIST_ENTRY(proc) p_list;
    TAILQ_HEAD(, thread) p_threads;
    int p_flag;
    pid_t p_pid;
    bogus_field_xyz garbage_xyzzy;
};
```
"""
proc_result = mod._verify_struct_bodies(
    mod._extract_struct_bodies(proc_draft), SRC,
)
if isinstance(proc_result, tuple):
    proc_issues, _proc_abridged = proc_result
else:
    proc_issues = proc_result
issue_text = " ".join(proc_issues)
# Each issue is "struct NAME: bogus, fields (real fields are: ...)". Only the
# part before "(real fields are:" names fields as WRONG — the parenthetical is
# the authoritative list handed to the writer so it can fix them in one step
# instead of looping (see _verify_struct_bodies). Match on the bogus half only,
# or every real field name trivially "appears in the issues".
bogus_text = " ".join(i.split("(real fields are:")[0] for i in proc_issues)
check(
    "real macro-bodied fields not flagged",
    "p_list" not in bogus_text and "p_threads" not in bogus_text
    and "p_flag" not in bogus_text and "p_pid" not in bogus_text,
    f"issues={proc_issues}",
)
check(
    "injected bogus field IS flagged",
    "garbage_xyzzy" in issue_text,
    f"issues={proc_issues}",
)
print()

print("=" * 60)
if failures:
    print(f"FAILED: {len(failures)} test(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All tests passed.")
