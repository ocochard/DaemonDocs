# tests/fixtures

Frozen copies of generated chapter output.

## Why these exist

Most tests in this suite read `~/freebsd-src` **source code** — upstream
C that this project does not write and that changes only when the tree is
updated. Pinning against it is stable and deliberate.

A handful of tests instead read **generated chapter output** (the
`README*.md` files the pipeline writes). That couples the test suite to
whenever a chapter last regenerated, and it fails in two ways that both
look like code regressions but are not:

1. **Content drift.** Chapter prose is model-authored. On 2026-09-02 a
   ch7 regen swapped the Glossary term `PML4` for `PV list`, and
   `test_glossary_linker.py` started failing on a chapter that was
   perfectly fine.

2. **Pinned defects.** `test_jargon_gloss.py` asserts that ch11 uses
   `KVM` in prose with no Glossary to define it — a real defect the test
   exists to reproduce. The moment the pipeline *fixes* that defect, the
   test fails for having succeeded.

Freezing keeps the "pinned against real shipped text" property that
motivated those blocks while removing the dependency on a file some other
chapter owns.

## Contents

| fixture | frozen from | pinned by |
|---|---|---|
| `vm_README.snapshot.md` | `sys/vm/README.md` (ch7) | `test_glossary_linker.py` |
| `vm_README_bcache.snapshot.md` | `sys/vm/README_bcache.md` (ch11) | `test_jargon_gloss.py` |
| `geom_README.snapshot.md` | `sys/geom/README.md` | `test_doc_url_sanitizer.py` |
| `netgraph_README.snapshot.md` | `sys/netgraph/README.md` | `test_mermaid_sanitizer.py` |
| `usb_README.snapshot.md` | `sys/dev/usb/README.md` (ch40) | `test_extractor_english.py` |

All snapshots taken 2026-09-02.

A missing fixture is a **hard test failure**, not a skip. The
`~/freebsd-src` reads elsewhere in the suite skip when the tree is absent,
because that is a legitimate environment difference; a fixture is
committed alongside the test, so its absence means the repo is broken.

## Refreshing a fixture

Only when you *intend* to re-pin against newer output, and only after
reading the diff — a fixture change should be a deliberate commit, not a
side effect of a regen:

    cp ~/freebsd-src/sys/vm/README.md tests/fixtures/vm_README.snapshot.md

If a test then fails, that is the signal to look at: the pipeline's
behaviour on that chapter changed.

## Adding a test that needs generated output

Freeze it here rather than reading `~/freebsd-src`. A test that reads a
live chapter will pass today and fail on the next unrelated regen.
