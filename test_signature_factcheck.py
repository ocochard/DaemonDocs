#!/usr/bin/env python3
"""Smoke test for the function-signature fact-check pass.

Run on bigone: `python3 test_signature_factcheck.py`.
Imports generate-doc.py via importlib (the hyphen in the filename
prevents normal `import`). Exits non-zero on any test failure.

Tests two layers:
  - `_count_c_args` (pure logic on parameter-list strings)
  - `_extract_function_signatures` (parsing fenced markdown blocks)

The end-to-end `_real_function_signature` lookup against the real
FreeBSD tree is exercised by the integration runs, not here — it
needs `FREEBSD_SRC` and live grep, which makes for a slow unit test
and doesn't catch parser bugs that this file does.
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


# ---------------------------------------------------------------------------
# Test 1: _count_c_args — pure-logic arity counting
# ---------------------------------------------------------------------------
print("Test 1: _count_c_args arity counting")

cases = [
    # (input arglist, expected arity, description)
    ("",                                            0, "empty arg list"),
    ("void",                                        0, "explicit void"),
    ("  void  ",                                    0, "void with whitespace"),
    ("const void",                                  0, "const void"),
    ("int a",                                       1, "single typed arg"),
    ("int a, int b",                                2, "two simple args"),
    ("struct bar *b, const char *s",                2, "struct ptr + const ptr"),
    ("struct bar *b, const char *s, int n",         3, "three args"),
    (
        "int (*cmp)(void *, void *), int n",
        2,
        "function pointer + int — nested commas should not count",
    ),
    (
        "int (*cmp)(void *, void *, int), int n, void *cookie",
        3,
        "function pointer with three nested args + 2 outer args",
    ),
    (
        "struct mtx *m, int flags, const char *file, int line",
        4,
        "WITNESS-style mutex args",
    ),
    (
        "/* comment */ int a /* mid */, int b // trailing\n",
        2,
        "args with C comments — must be stripped first",
    ),
    (
        "struct thread *td, struct fork_args *uap",
        2,
        "real FreeBSD syscall signature",
    ),
    (
        "int a, int b, ...",
        3,
        "variadic — explicit args plus the `...` slot",
    ),
]

for arglist, expected, desc in cases:
    got = mod._count_c_args(arglist)
    check(
        f"_count_c_args({arglist!r}) == {expected}",
        got == expected,
        f"got {got!r} for {desc}",
    )

# K&R-style — bare identifiers, no types. Must return None so the
# verifier knows verification is unavailable and skips the claim.
kr_cases = [
    "td, args",
    "a, b, c",
    "x",
]
for arglist in kr_cases:
    got = mod._count_c_args(arglist)
    check(
        f"_count_c_args({arglist!r}) is None (K&R)",
        got is None,
        f"got {got!r}",
    )

print()


# ---------------------------------------------------------------------------
# Test 2: _extract_function_signatures — fenced-block parsing
# ---------------------------------------------------------------------------
print("Test 2: _extract_function_signatures from markdown")

# Single fenced block, single function definition, arity 2.
draft_one = """
Some prose here.

```c
int
daemon_init(struct config *conf, bool debug)
{
    /* body */
}
```

More prose.
"""
claims = mod._extract_function_signatures(draft_one)
check(
    "single defn extracts as one claim",
    len(claims) == 1,
    f"got {len(claims)} claims",
)
if claims:
    name, arity, _raw = claims[0]
    check(
        "extracted name == daemon_init",
        name == "daemon_init",
        f"got {name!r}",
    )
    check(
        "extracted arity == 2",
        arity == 2,
        f"got {arity!r}",
    )

# Multiple definitions in one block.
draft_multi = """
```c
static int
foo(void)
{
    return 0;
}

void *
bar(int a, struct s *s)
{
    return NULL;
}
```
"""
claims = mod._extract_function_signatures(draft_multi)
names_arities = {(n, a) for n, a, _ in claims}
check(
    "two defs in one block",
    {("foo", 0), ("bar", 2)} <= names_arities,
    f"got {names_arities!r}",
)

# Inline backticked calls must NOT be extracted.
draft_inline_only = """
The function `daemon_init(conf, true)` is called from `main()`.
No code fences here.
"""
claims = mod._extract_function_signatures(draft_inline_only)
check(
    "inline backticked calls produce no claims",
    len(claims) == 0,
    f"got {len(claims)} claims: {claims!r}",
)

# Control-flow keywords (if, while, for, switch) must NOT be flagged
# as function names even though their syntax matches `RETTY NAME(...) {`.
draft_keywords = """
```c
if (cond) {
    return 1;
}
while (n--) {
    foo();
}
```
"""
claims = mod._extract_function_signatures(draft_keywords)
names = {n for n, _, _ in claims}
check(
    "control-flow keywords are not function names",
    not (names & {"if", "while", "for", "switch", "do"}),
    f"got names {names!r}",
)

# K&R-style draft signature — extractor should skip it (verifier
# would not be able to count arity reliably).
draft_kr = """
```c
int
foo(td, args)
struct thread *td;
struct fork_args *args;
{
    /* body */
}
```
"""
claims = mod._extract_function_signatures(draft_kr)
check(
    "K&R-style definition produces no claim",
    len(claims) == 0,
    f"got {len(claims)} claims",
)

# Dedup: same name+arity twice should appear once.
draft_dup = """
```c
int foo(int a) { return a; }
```

```c
int foo(int a) { return a; }
```
"""
claims = mod._extract_function_signatures(draft_dup)
check(
    "dedup on (name, arity)",
    len(claims) == 1,
    f"got {len(claims)} claims",
)

# Function-pointer arg counted as one — the inner commas shouldn't
# leak into the outer arity.
draft_funcptr = """
```c
int qsort_thing(int (*cmp)(void *, void *), int n) { return 0; }
```
"""
claims = mod._extract_function_signatures(draft_funcptr)
check(
    "function-pointer arg counted as one (outer arity 2)",
    len(claims) == 1 and claims[0][1] == 2,
    f"got {claims!r}",
)

# Variadic explicit prefix counted plus the `...` slot.
draft_var = """
```c
int printf(const char *fmt, ...) { return 0; }
```
"""
claims = mod._extract_function_signatures(draft_var)
check(
    "variadic counts explicit args + ... slot (arity 2)",
    len(claims) == 1 and claims[0][1] == 2,
    f"got {claims!r}",
)

# Definition that spans multiple lines (the K&R-shape return type on
# its own line followed by name at column 0).
draft_multiline = """
```c
struct inpcb *
in_pcblookup_hash(struct in_addr faddr, u_short fport,
                  struct in_addr laddr, u_short lport,
                  int flags, struct ifnet *ifp)
{
    /* body */
}
```
"""
claims = mod._extract_function_signatures(draft_multiline)
check(
    "multi-line signature parses correctly",
    len(claims) == 1 and claims[0][0] == "in_pcblookup_hash"
    and claims[0][1] == 6,
    f"got {claims!r}",
)

# Forward declaration (no body brace) must NOT be extracted — we
# only want definitions, since prototypes can drift independently of
# the real implementation and arity-checking them is noisier.
draft_proto = """
```c
extern int daemon_init(struct config *conf, bool debug);
```
"""
claims = mod._extract_function_signatures(draft_proto)
check(
    "forward declaration (no brace) not extracted",
    len(claims) == 0,
    f"got {len(claims)} claims: {claims!r}",
)

print()


# ---------------------------------------------------------------------------
# Test 3: _verify_function_signatures — collector logic
# ---------------------------------------------------------------------------
print("Test 3: _verify_function_signatures skip rules")

# Patch `_real_function_signature` for the duration of the test so
# we don't depend on the real source tree. This exercises the
# collector logic, not the lookup.
orig_lookup = mod._real_function_signature


def fake_lookup(name, src_root, extra_search_dirs=None):
    table = {
        "daemon_init":   (2, "sys/kern/kern_daemon.c"),
        "fork1":         (2, "sys/kern/kern_fork.c"),
        "thr_create":    (5, "sys/kern/kern_thr.c"),
        "vanished_func": None,  # not found in tree
    }
    return table.get(name)


mod._real_function_signature = fake_lookup
try:
    claims = [
        ("daemon_init",    0, "raw1"),  # mismatch — real has 2
        ("fork1",          2, "raw2"),  # match — no flag
        ("thr_create",     3, "raw3"),  # mismatch — real has 5
        ("vanished_func",  1, "raw4"),  # lookup None — skip silently
        ("already_missing", 0, "raw5"),  # already in funcs_missing — skip
    ]
    issues = mod._verify_function_signatures(
        claims, "/fake/src",
        funcs_missing_set={"already_missing"},
    )
    issue_str = "\n".join(issues)
    check(
        "daemon_init mismatch flagged",
        any("daemon_init" in i and "0 arg" in i and "2 arg" in i
            for i in issues),
        issue_str,
    )
    check(
        "thr_create mismatch flagged",
        any("thr_create" in i and "3 arg" in i and "5 arg" in i
            for i in issues),
        issue_str,
    )
    check(
        "fork1 match silent (no issue)",
        not any("fork1" in i for i in issues),
        issue_str,
    )
    check(
        "lookup-None skipped silently",
        not any("vanished_func" in i for i in issues),
        issue_str,
    )
    check(
        "already-missing skipped (no double-report)",
        not any("already_missing" in i for i in issues),
        issue_str,
    )
    check(
        "exactly two issues emitted",
        len(issues) == 2,
        f"got {len(issues)}: {issues!r}",
    )
finally:
    mod._real_function_signature = orig_lookup

print()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nAll signature fact-check tests passed.")
