#!/usr/bin/env python3
"""Smoke test for the mermaid post-process sanitizer.

Run on framework: `python3 test_mermaid_sanitizer.py`.
Exits non-zero on any test failure.
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


# 1) The actual netgraph fixture from sys/netgraph/README.md.
print("Test 1: netgraph fixture — Userland node + Userland subgraph")
netgraph = """\
```mermaid
flowchart TD
    Userland["Userland (ngctl / libnetgraph)"] -->|Socket I/O| NgSocket["ng_socket /dev/ng"]
    NgSocket -->|Messages| NgBase["ng_base.c: Message Router"]

    subgraph Kernel
        NgSocket
        NgBase
    end

    subgraph Userland
        Userland
    end
```
"""
out = mod._sanitize_mermaid_blocks(netgraph)
check(
    "subgraph Userland renamed",
    "subgraph Userland\n" not in out and "subgraph Userland_grp" in out,
    f"diff snippet:\n{out}",
)
check(
    "node id Userland preserved",
    'Userland["Userland (ngctl / libnetgraph)"]' in out,
)
check(
    "Kernel subgraph untouched (no collision)",
    "subgraph Kernel\n" in out,
)
print()

# 2) Idempotent.
print("Test 2: sanitizer is idempotent")
out2 = mod._sanitize_mermaid_blocks(out)
check(
    "second pass yields identical output",
    out == out2,
)
print()

# 3) No-op on a clean diagram.
print("Test 3: clean diagram passes through unchanged")
clean = """\
```mermaid
flowchart TD
    A["Apple"] --> B["Banana"]
    subgraph Fruits
        A
        B
    end
```
"""
out3 = mod._sanitize_mermaid_blocks(clean)
check("clean diagram unchanged", out3 == clean)
print()

# 4) No-op on non-flowchart blocks (sequence, class, etc.).
print("Test 4: non-flowchart mermaid blocks pass through")
seq = """\
```mermaid
sequenceDiagram
    Alice->>Bob: Hi
    Bob->>Alice: Hello
```
"""
out4 = mod._sanitize_mermaid_blocks(seq)
check("sequenceDiagram unchanged", out4 == seq)

cls = """\
```mermaid
classDiagram
    class Foo {
        +int bar
    }
```
"""
out5 = mod._sanitize_mermaid_blocks(cls)
check("classDiagram unchanged", out5 == cls)
print()

# 5) Multiple collisions in the same diagram.
print("Test 5: multiple collisions all renamed")
multi = """\
```mermaid
flowchart TD
    A["Alpha"] --> B["Beta"]
    C["Gamma"] --> A

    subgraph A
        B
    end

    subgraph C
        A
    end
```
"""
out6 = mod._sanitize_mermaid_blocks(multi)
check(
    "subgraph A renamed",
    "subgraph A_grp" in out6 or "subgraph A_grp2" in out6,
)
check(
    "subgraph C renamed",
    "subgraph C_grp" in out6 or "subgraph C_grp2" in out6,
)
check(
    "no bare `subgraph A\\n` left",
    "subgraph A\n" not in out6,
)
check(
    "no bare `subgraph C\\n` left",
    "subgraph C\n" not in out6,
)
print()

# 6) Subgraph with explicit title — title is preserved.
print("Test 6: subgraph with quoted title")
titled = """\
```mermaid
flowchart TD
    Userland["Userland"] --> X
    subgraph Userland ["The User Side"]
        X
    end
```
"""
out7 = mod._sanitize_mermaid_blocks(titled)
check(
    "renamed but title preserved",
    'subgraph Userland_grp ["The User Side"]' in out7,
    f"got:\n{out7}",
)
print()

# 7) End-to-end on the actual file from framework's freebsd-src.
#    Two valid states for this file: (a) still has the original
#    collision and the sanitizer rewrites it, or (b) already
#    patched (idempotent — sanitizer leaves it alone). Either way,
#    the post-condition is the same: no bare collision remains and
#    the renamed subgraph is present.
fpath = os.path.expanduser("~/freebsd-src/sys/netgraph/README.md")
if os.path.isfile(fpath):
    print(f"Test 7: end-to-end on {fpath}")
    text = open(fpath).read()
    fixed = mod._sanitize_mermaid_blocks(text)
    # Post-condition holds for BOTH valid states (see comment above):
    #   (a) the file still had the `Userland` node/subgraph collision and
    #       the sanitizer rewrote the subgraph to `Userland_grp`; or
    #   (b) the file was regenerated with a collision-free diagram (a
    #       distinct subgraph id such as `subgraph UserlandGroup
    #       ["Userland"]`), so there is nothing to rename.
    # The invariant common to both — and the thing the sanitizer actually
    # guarantees — is that no bare node/subgraph id collision survives and
    # the sanitizer is idempotent. Asserting `Userland_grp` must be present
    # only held in case (a) and broke on the first clean regen.
    check(
        "no bare 'subgraph Userland' collision remains",
        "subgraph Userland\n" not in fixed,
    )
    check(
        "if a rename happened, the original id is preserved as a node",
        # Either the sanitizer renamed it (…_grp present) or the writer
        # avoided the collision entirely (…_grp absent). Both are fine;
        # what must NOT happen is a rename that dropped the visible label.
        "subgraph Userland_grp" not in fixed
        or 'Userland["Userland' in fixed
        or "Userland_grp [" in fixed,
    )
    check(
        "sanitizer is idempotent on the real file",
        mod._sanitize_mermaid_blocks(fixed) == fixed,
    )
    print()
else:
    print(f"(skipped Test 7 — {fpath} not present in this checkout)")
    print()

# 8) Non-mermaid content with `subgraph` text in prose is untouched.
print("Test 8: prose mentioning 'subgraph' is untouched")
prose = "Some chapters discuss `subgraph` as a Mermaid keyword."
out8 = mod._sanitize_mermaid_blocks(prose)
check("prose unchanged", out8 == prose)
print()

print("=" * 60)
if failures:
    print(f"FAILED: {len(failures)} test(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All tests passed.")
