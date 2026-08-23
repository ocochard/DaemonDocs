#!/usr/bin/env python3
"""End-to-end tests for the three new hallucination-catching extractors.

Motivated by ch2 (stand/efi/loader/README.md, 2026-05-02 K8 regen) that
shipped UNVERIFIED with invented FreeBSD symbols (`bi_efi_memmap`,
`bi_efi_memmap_size`, `bi_modlist`, `bi_construct()`) presented as
authoritative. The previous fact-check pipeline missed all of them.

Run on framework (the host with ~/freebsd-src):
    python3 test_hallucination_factcheck.py

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


# Synthetic fixture mirroring the ch2 hallucinations. We don't load the
# 389-line real chapter — that would couple the test to file state on
# framework. The patterns below are the exact shapes the chapter used.
CH2_FIXTURE = r"""
# The Boot Process: From UEFI Firmware to FreeBSD Kernel

## Quick Summary

The bootloader constructs a `struct bootinfo` containing the memory map,
loaded modules (`bi_module`), and boot arguments.

## Key Data Structures

### struct bootinfo

The `struct bootinfo` is the central data structure. It contains:

- **Memory Map**: The EFI memory map.
- **bi_module List**: A linked list of loaded modules.

3. **Save Memory Map**: The bootloader stores it in `bootinfo->bi_efi_memmap`.

The bootloader loads ELF kernel and module files. Each loaded file is
described by a `bi_module` structure added to the `bi_modlist` linked
list in `struct bootinfo`.

```c
static int
bi_construct(void)
{
    struct bootinfo *bi;
    EFI_MEMORY_DESCRIPTOR *memmap;

    bi = bootinfo_alloc(sizeof(struct bootinfo) + map_size);
    bi->bi_efi_memmap = (void *)(bi + 1);
    bi->bi_efi_memmap_size = map_size;
    bcopy(memmap, bi->bi_efi_memmap, map_size);

    return (0);
}
```

The kernel reads `bootinfo.bi_modulep` to find the module chain.
"""


# ---------------------------------------------------------------------
# Test 1: _extract_fenced_function_defs catches bi_construct()
# ---------------------------------------------------------------------
print("Test 1: fenced function-def extractor")
defs = mod._extract_fenced_function_defs(CH2_FIXTURE)
check(
    "bi_construct extracted from fenced ```c block",
    "bi_construct" in defs,
    f"got={defs}",
)
# Sanity: a control-flow keyword would not match (no fixture line uses
# `if (...) {` at start-of-line as a definition shape).
print()


# ---------------------------------------------------------------------
# Test 2: _extract_function_names UNIONS in fenced defs (ch2's fix)
# ---------------------------------------------------------------------
print("Test 2: _extract_function_names includes fenced defs")
names = mod._extract_function_names(CH2_FIXTURE)
check(
    "bi_construct surfaces from full extractor",
    "bi_construct" in names,
    f"got={names}",
)
print()


# ---------------------------------------------------------------------
# Test 3: _verify_functions correctly reports bi_construct as missing
# ---------------------------------------------------------------------
print("Test 3: _verify_functions rejects bi_construct against real tree")
missing = mod._verify_functions(["bi_construct"], SRC)
check(
    "bi_construct flagged as missing",
    "bi_construct" in missing,
    f"missing={missing}",
)
# Negative: a real, well-known function must NOT be flagged.
real_missing = mod._verify_functions(["malloc"], SRC)
check(
    "malloc is NOT flagged (sanity)",
    "malloc" not in real_missing,
    f"missing={real_missing}",
)
print()


# ---------------------------------------------------------------------
# Test 4: _extract_struct_field_claims catches member-access in code
# ---------------------------------------------------------------------
print("Test 4: struct field-ref extractor (code-block member access)")
# Scoped fixture: the `bi` declaration must be in the same fenced block
# as the member-access lines. The CH2_FIXTURE block has both.
claims = mod._extract_struct_field_claims(CH2_FIXTURE, known_structs=["bootinfo"])
claim_set = set(claims)
check(
    "(bootinfo, bi_efi_memmap) extracted from `bi->bi_efi_memmap`",
    ("bootinfo", "bi_efi_memmap") in claim_set,
    f"claims={claims}",
)
check(
    "(bootinfo, bi_efi_memmap_size) extracted from `bi->bi_efi_memmap_size`",
    ("bootinfo", "bi_efi_memmap_size") in claim_set,
    f"claims={claims}",
)
print()


# ---------------------------------------------------------------------
# Test 5: prose `STRUCTNAME->FIELD` form is also caught
# ---------------------------------------------------------------------
print("Test 5: struct field-ref extractor (prose paraphrase form)")
# `bootinfo->bi_efi_memmap` appears in prose at "stored in
# `bootinfo->bi_efi_memmap`". `bootinfo` is in the chapter's known
# struct set (extracted by _extract_struct_names from `struct bootinfo`).
known = mod._extract_struct_names(CH2_FIXTURE)
check(
    "_extract_struct_names finds 'bootinfo'",
    "bootinfo" in known,
    f"known={known}",
)
prose_claims = mod._extract_struct_field_claims(
    CH2_FIXTURE, known_structs=known,
)
prose_set = set(prose_claims)
check(
    "(bootinfo, bi_efi_memmap) extracted from prose form",
    ("bootinfo", "bi_efi_memmap") in prose_set,
    f"claims={prose_claims}",
)
print()


# ---------------------------------------------------------------------
# Test 6: _verify_struct_field_claims rejects against real bootinfo
# ---------------------------------------------------------------------
print("Test 6: _verify_struct_field_claims against real struct bootinfo")
issues = mod._verify_struct_field_claims(
    [("bootinfo", "bi_efi_memmap"),
     ("bootinfo", "bi_efi_memmap_size"),
     ("bootinfo", "bi_modulep")],   # last one IS real
    SRC,
)
check(
    "bi_efi_memmap flagged as bogus",
    any("bi_efi_memmap" in i and "size" not in i for i in issues),
    f"issues={issues}",
)
check(
    "bi_efi_memmap_size flagged as bogus",
    any("bi_efi_memmap_size" in i for i in issues),
    f"issues={issues}",
)
check(
    "bi_modulep is NOT flagged (real field)",
    not any("bi_modulep" in i for i in issues),
    f"issues={issues}",
)
print()


# ---------------------------------------------------------------------
# Test 7: _extract_struct_names catches "a `bi_module` structure"
# ---------------------------------------------------------------------
print("Test 7: backticked-prose struct extractor")
known = mod._extract_struct_names(CH2_FIXTURE)
check(
    "'bi_module' extracted from `bi_module` structure",
    "bi_module" in known,
    f"known={known}",
)
# Negative: 'data' must not surface from "data structure" (English).
plain_prose = "This is a tree structure used as a data structure."
no_struct = mod._extract_struct_names(plain_prose)
check(
    "common-English 'data structure' not extracted",
    "data" not in no_struct and "tree" not in no_struct,
    f"got={no_struct}",
)
print()


# ---------------------------------------------------------------------
# Test 8: end-to-end fact_check_draft surfaces all hallucinations
# ---------------------------------------------------------------------
print("Test 8: fact_check_draft on the ch2 fixture")
res = mod.fact_check_draft(CH2_FIXTURE, SRC)
check(
    "result has struct_field_refs_bogus key",
    "struct_field_refs_bogus" in res,
)
field_refs = res.get("struct_field_refs_bogus", [])
check(
    "struct_field_refs_bogus flags bi_efi_memmap",
    any("bi_efi_memmap" in s and "size" not in s for s in field_refs),
    f"field_refs={field_refs}",
)
check(
    "struct_field_refs_bogus flags bi_efi_memmap_size",
    any("bi_efi_memmap_size" in s for s in field_refs),
    f"field_refs={field_refs}",
)
check(
    "funcs_not_found flags bi_construct",
    "bi_construct" in res.get("funcs_not_found", []),
    f"funcs_not_found={res.get('funcs_not_found')}",
)
check(
    "structs_not_found flags bi_module (the fictional struct)",
    "bi_module" in res.get("structs_not_found", []),
    f"structs_not_found={res.get('structs_not_found')}",
)
check(
    "total_issues > 0",
    res["total_issues"] > 0,
    f"total={res['total_issues']}",
)
print()


# ---------------------------------------------------------------------
# Test 9: NEGATIVE — clean draft against ch26 mbuf must not false-positive
# ---------------------------------------------------------------------
print("Test 9: real `m->m_next` and `m->m_data` must not be flagged")
clean_draft = r"""
## struct mbuf

```c
struct mbuf {
    union {
        struct mbuf *m_next;
        SLIST_ENTRY(mbuf) m_slist;
    };
    caddr_t m_data;
    int32_t m_len;
};
```

The `m_next` pointer chains the buffer. Access via `m->m_next` and
`m->m_data` follows the chain.
"""
clean_known = mod._extract_struct_names(clean_draft)
clean_claims = mod._extract_struct_field_claims(
    clean_draft, known_structs=clean_known,
)
clean_issues = mod._verify_struct_field_claims(clean_claims, SRC)
check(
    "no false positives on real mbuf field accesses",
    clean_issues == [],
    f"issues={clean_issues}",
)
# Also: the `m_next` field is real, so it should pass struct-body verify.
res2 = mod.fact_check_draft(clean_draft, SRC)
check(
    "fact_check_draft on real mbuf yields 0 struct_field_refs_bogus",
    res2.get("struct_field_refs_bogus") == [],
    f"value={res2.get('struct_field_refs_bogus')}",
)
print()


# ---------------------------------------------------------------------
# Test 9b: NEGATIVE — `bootinfo.c` (file path) must not be `bootinfo.c`
#                     misread as struct field access
# ---------------------------------------------------------------------
print("Test 9b: file-path extensions don't false-positive as field access")
path_prose = "See `stand/efi/loader/bootinfo.c` and `sys/sys/buf.h` for details."
known = ["bootinfo", "buf"]
path_claims = mod._extract_struct_field_claims(path_prose, known_structs=known)
check(
    "bootinfo.c not extracted as (bootinfo, c)",
    ("bootinfo", "c") not in path_claims,
    f"claims={path_claims}",
)
check(
    "buf.h not extracted as (buf, h)",
    ("buf", "h") not in path_claims,
    f"claims={path_claims}",
)
print()


# ---------------------------------------------------------------------
# Test 10: NEGATIVE — function-def extractor doesn't fire on calls
# ---------------------------------------------------------------------
print("Test 10: fenced function-def extractor skips call sites")
call_only = """
```c
err = bi_load(fp->f_args, &modulep, &kernendp, true);
malloc(size);
free(p);
```
"""
defs = mod._extract_fenced_function_defs(call_only)
check(
    "no defs extracted from a call-only block",
    defs == [],
    f"got={defs}",
)
print()


# ---------------------------------------------------------------------
# Test 11: extra_search_dirs — stand-only struct verifies only when opted in
# ---------------------------------------------------------------------
# `struct preloaded_file` is defined in `stand/common/bootstrap.h`, NOT
# under sys/. With the default sys-only search root the verifier cannot
# find it; with `extra_search_dirs=["stand"]` it should. (Note: the
# enclosing `_verify_structs` shape-grep currently uses `^struct NAME *\{`
# which doesn't match K&R-style brace-on-next-line definitions; we test
# against `_real_struct_fields` directly which uses `_extract_struct_body`
# and so handles either brace style.)
print("Test 11: extra_search_dirs lets stand-only structs verify")
fields_default = mod._real_struct_fields("preloaded_file", SRC)
fields_extras = mod._real_struct_fields("preloaded_file", SRC, ["stand"])
check(
    "preloaded_file NOT findable with sys-only roots",
    fields_default == set(),
    f"fields_default={sorted(fields_default)[:6]}...",
)
check(
    "preloaded_file IS findable with extra_search_dirs=['stand']",
    len(fields_extras) >= 4,
    f"len(fields)={len(fields_extras)}",
)
# Cache key isolation: a cached miss for sys-only must NOT poison the
# stand-included lookup. (We just exercised both above; the check is
# that the second call returned non-empty even though the first was
# cached as empty.)
check(
    "cache-key isolation: extras lookup wasn't poisoned by sys-only miss",
    len(fields_extras) > len(fields_default),
    f"default={len(fields_default)} vs extras={len(fields_extras)}",
)
print()


# ---------------------------------------------------------------------
# Test 11b: K&R-brace gap — `struct NAME\n{` definitions must verify.
# `stand/common/bootstrap.h:230` writes `struct preloaded_file\n{` on
# its own line, with `{` on the next. The shape-grep used to require
# `^struct NAME *\{` (same-line brace) and false-flagged the K&R form
# as missing, which then caused the reviewer to downgrade Accuracy on
# ch2 (Boot Process) even though the chapter named real symbols.
# 1199+ structs in sys/ alone use this style.
# ---------------------------------------------------------------------
print("Test 11b: K&R-brace `struct NAME\\n{` definitions verify")
# `struct preloaded_file` is K&R-style and lives under stand/.
missing_sys_only = mod._verify_structs(
    ["proc", "preloaded_file", "file_metadata", "mbuf"], SRC,
)
# `proc` and `mbuf` are sys/ same-line-brace; `preloaded_file` and
# `file_metadata` are stand/ K&R. With sys-only roots the latter two
# are correctly reported missing.
check(
    "sys-only: K&R same-line `struct proc {` is found",
    "proc" not in missing_sys_only,
    f"missing={missing_sys_only}",
)
check(
    "sys-only: stand-only structs ARE missing without extra_search_dirs",
    "preloaded_file" in missing_sys_only
    and "file_metadata" in missing_sys_only,
    f"missing={missing_sys_only}",
)
# With `stand` opted in, the K&R-brace structs verify clean.
missing_with_stand = mod._verify_structs(
    ["preloaded_file", "file_metadata"], SRC, ["stand"],
)
check(
    "extra_search_dirs=['stand']: K&R-brace structs found",
    missing_with_stand == [],
    f"missing={missing_with_stand}",
)
# Clearly fabricated struct must still be flagged even with stand opt-in.
bogus_missing = mod._verify_structs(["bi_module_xyz_fake"], SRC, ["stand"])
check(
    "fabricated struct still flagged with stand widening",
    "bi_module_xyz_fake" in bogus_missing,
    f"missing={bogus_missing}",
)
print()


# ---------------------------------------------------------------------
# Test 12: abridged-struct check — zero-overlap body flagged when no
# abridgement marker; NOT flagged when /* ... */ is present.
# ---------------------------------------------------------------------
print("Test 12: zero-overlap struct body is flagged as abridged")
# Use struct mbuf — its real top-level fields include `m_next`, `m_nextpkt`,
# `m_data`, `m_len`, etc. Below we name 5 plausible-but-fake fields that
# share NO overlap with the real definition.
zero_overlap_draft = r"""
```c
struct mbuf {
    void *mb_buffer;
    int mb_size;
    int mb_offset;
    struct mbuf *mb_link;
    int mb_flags;
};
```
"""
claims = mod._extract_struct_bodies(zero_overlap_draft)
check(
    "extracted exactly 1 mbuf claim",
    len(claims) == 1 and claims[0][0] == "mbuf",
    f"claims={[c[0] for c in claims]}",
)
bogus, abridged = mod._verify_struct_bodies(claims, SRC)
check(
    "zero-overlap body flagged as abridged",
    any("mbuf" in s for s in abridged),
    f"abridged={abridged}",
)
print()

# Same draft with `/* ... */` elision: the abridged-marker check should
# now SKIP the overlap test (the writer is honestly signalling elision).
print("Test 12b: explicit `/* ... */` elision is NOT flagged")
elided_draft = r"""
```c
struct mbuf {
    void *mb_buffer;
    /* ... */
};
```
"""
elided_claims = mod._extract_struct_bodies(elided_draft)
_e_bogus, e_abridged = mod._verify_struct_bodies(elided_claims, SRC)
check(
    "elided body NOT flagged as abridged",
    not any("mbuf" in s for s in e_abridged),
    f"abridged={e_abridged}",
)
print()

# Negative case: the real mbuf definition (a few real fields) must NOT
# trigger the abridged check — at least one real top-level field is
# present, so overlap > 0.
print("Test 12c: real-field body is NOT flagged as abridged")
real_draft = r"""
```c
struct mbuf {
    struct mbuf *m_next;
    struct mbuf *m_nextpkt;
    caddr_t m_data;
    int m_len;
};
```
"""
real_claims = mod._extract_struct_bodies(real_draft)
_r_bogus, r_abridged = mod._verify_struct_bodies(real_claims, SRC)
check(
    "real-field body NOT flagged as abridged",
    not any("mbuf" in s for s in r_abridged),
    f"abridged={r_abridged}",
)
print()


# ---------------------------------------------------------------------
# Test 13: fact_check_draft surfaces struct_bodies_abridged
# ---------------------------------------------------------------------
print("Test 13: fact_check_draft exposes struct_bodies_abridged key")
fc = mod.fact_check_draft(zero_overlap_draft, SRC)
check(
    "result has struct_bodies_abridged key",
    "struct_bodies_abridged" in fc,
    f"keys={list(fc.keys())[:8]}...",
)
check(
    "struct_bodies_abridged is non-empty for zero-overlap draft",
    len(fc.get("struct_bodies_abridged", [])) >= 1,
    f"value={fc.get('struct_bodies_abridged')}",
)
check(
    "total_issues includes the abridged count",
    fc["total_issues"] >= 1,
    f"total={fc['total_issues']}",
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
