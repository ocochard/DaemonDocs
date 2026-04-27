# FreeBSD Internals

[![GitHub](https://img.shields.io/badge/GitHub-DaemonDocs-181717?logo=github)](https://github.com/ocochard/DaemonDocs)

AI-generated documentation of FreeBSD internals. Each chapter produces a markdown file placed directly in the relevant FreeBSD source directory — so anyone can `git clone` the tree and find educational material right next to the code. Outputs use a `README_*.md` naming convention (e.g. `README_internals.md`, `README_process.md`) so they never clobber FreeBSD's upstream `README.md` files.

## Vision

Build documentation of FreeBSD internals by having AI agents study the FreeBSD source code and cross-reference it with FreeBSD books. The output is a set of `README_*.md` files placed throughout the FreeBSD source tree — so any reader can `git clone` the tree and find educational material right next to the code.

The reference works are:
- *The Design and Implementation of FreeBSD* (McKusick et al.)
- *FreeBSD Device Drivers* (Pfeffer et al.)
- *Designing BSD: Rootkits* (for kernel hacking concepts)

The goal is **not** to reproduce man pages. The goal is to help anyone who knows C but has never touched a kernel — students, developers, hobbyists — understand how an operating system actually works by studying real, shipping code.

---

## What the system does

1. **Extracts text** from FreeBSD books (PDF/CHM/EPUB) in `$HOME/books/`
2. **Adds FreeBSD's own docs** — man9 pages, Handbook articles, git commit logs, technical papers, kerneldoc
3. **Builds a TF-IDF search index** over the combined corpus (numpy only)
4. **For each chapter** in `chapters.yaml`:
   - A writer agent studies the source code and searches the corpus
   - A reviewer agent grades the draft on 7 criteria (including a check that no marketing language slipped in)
   - If needed, the writer revises — up to `--max-revisions` rounds
5. **Writes** the final markdown file into the relevant source directory (e.g. `README_internals.md`, `sys/vm/README_vm.md`)

---

## Prerequisites

All four resources must live on (or be reachable from) the same host where you run `generate-doc.py`:

1. This repo
2. The FreeBSD source tree (`$FREEBSD_SRC`)
3. The books corpus (`$BOOKS_DIR`)
4. A running llama-server (see config below)

Running with any of them missing or remote will silently degrade output quality.

### LLM configuration

The script talks to an OpenAI-compatible local server. Defaults are set in `MODEL_CONFIG` near the top of `generate-doc.py`:

| Setting | Value |
|---|---|
| Endpoint | `http://localhost:8080/v1` |
| Model id | `qwen36-coder` |
| API key | `none` (llama-server ignores it) |

The `model_id` must match exactly what `llama-server` advertises at `/v1/models`. A mismatch causes the request to fail or to silently route to whichever model is loaded — the output looks plausible but quality drops.

## Quick start

```sh
# 1. Set environment (if not already)
export FREEBSD_SRC=$HOME/freebsd-src
export BOOKS_DIR=$HOME/books
export FREEBSD_DOC=$HOME/freebsd-doc/documentation/content/en

# 2. Install dependencies
python3 -m pip install --user -r requirements.txt

# 3. Verify the LLM endpoint
curl -s http://localhost:8080/v1/models | grep qwen36-coder

# 4. Build corpus index (books + FreeBSD docs + git logs)
python3 generate-doc.py --index-only

# 5. Dry run — confirms all chapters resolve without calling the LLM
python3 generate-doc.py --dry-run

# 6. Smoke-test a single chapter end-to-end
python3 generate-doc.py --chapter 1

# 7. Full run (use --max-revisions 3 for production-quality output)
python3 generate-doc.py --max-revisions 3

# 8. Refresh cross-README navigation links
python3 generate-doc.py --nav-only
```

For lowest error rate, follow steps 4–8 in order on a fresh run. Skipping the smoke test is the most common cause of wasting a long full-corpus run.

---

## CLI options

| Flag | Description |
|---|---|
| `--index-only` | Build corpus + index, exit (don't run agents) |
| `--nav-only` | Rebuild cross-README navigation links only |
| `--index` | Rebuild CHAPTER_INDEX.md (TOC, glossary, cross-refs) only |
| `--dry-run` | Show what would happen without running agents |
| `--force` | Regenerate even if README already exists |
| `--reindex` | Rebuild corpus from scratch (drops existing docs) |
| `--chapter N` | Run only chapter N (1-based) |
| `--max-revisions N` | Max review+revise rounds (default 2, 0 = skip review) |

> **`--force` safety:** output files are written under `$FREEBSD_SRC`. `--force` will overwrite any
> previously generated `README.md` / `README_*.md` listed in `chapters.yaml`. Run
> `git -C $FREEBSD_SRC status` before and after a forced run to review the diff. The generator only
> writes paths declared in `chapters.yaml` — it does not touch other files in the source tree.

---

## Known error modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Reviewer JSON parse fails | model output truncated mid-object | lower temperature, raise context, or rerun the chapter |
| Draft references nonexistent paths | `$FREEBSD_SRC` is stale | `git -C $FREEBSD_SRC pull` then `--reindex` |
| Empty `search_books` results | corpus index not built or stale | run `--index-only` (or `--reindex` after adding books) |
| Same chapter regenerates each run | `--force` left in the command line | drop the flag once a chapter passes review |
| HTTP 404 on `/v1/chat/completions` | wrong model loaded in llama-server | reload llama-server with `qwen36-coder` (must match `MODEL_CONFIG`) |
| Connection refused on port 8080 | llama-server not running | start it before launching `generate-doc.py` |

---

## Corpus sources

The TF-IDF index covers:
- **FreeBSD books** — PDFs from `$HOME/books/` (McKusick, Device Drivers, etc.)
- **Man9 pages** — `share/man/man9/*` from the source tree (475+ kernel API docs)
- **Handbook & articles** — `$HOME/freebsd-doc/documentation/content/en/` (186 AsciiDoc files)
- **Git commit logs** — `git log --follow` of 17 key kernel files (developer design rationale)
- **Technical papers** — `share/doc/papers/*` (bufbio, newvm, jail, sysperf)
- **Kerneldoc** — `tools/kerneldoc/Doxyfile-*` (subsystem descriptions)

---

## Architecture of `generate-doc.py`

The script is **one self-contained file** — no separate modules. Sections:

| Section | What it does |
|---|---|
| Config | `SRC_ROOT`, `BOOKS_DIR`, `MODEL_CONFIG` |
| Book extraction | Pull text from PDFs (PyPDF2), CHMs (hhextract), EPUBs (zipfile). Incremental by file hash. |
| TF-IDF index | Chunks text, builds vocabulary, computes TF-IDF matrix with numpy, cosine similarity search. Save/load to disk. |
| smolagent tools | `ReadFreeBSDSource`, `SearchBooks`, `ExploreTree`, `ResolveCDefinition` |
| Prompt builders | `build_chapter_prompt`, `build_review_prompt`, `build_revision_prompt`, `build_chapter_index` |
| Agent factories | `create_writer_agent`, `create_reviewer_agent` |
| Orchestrator | Multi-pass loop: draft → review → revise → write file |

---

## The four agent tools

### `read_freebsd_source(path)`
Reads a file from the FreeBSD source tree. Returns up to 4000 chars. If the path doesn't exist, tries glob for similar files.

### `search_books(query)`
TF-IDF semantic search over the book corpus. Returns top-4 matching chunks with source attribution.

### `explore_tree(path)`
Lists directory contents in the FreeBSD source tree. Shows files and directories (up to 80 entries).

### `resolve_c_definition(symbol, start_file="")`
Finds the definition of a C struct, function, macro, or type alias. Follows `#include` chains automatically. Examples: `resolve_c_definition(symbol='struct vm_page')`, `resolve_c_definition(symbol='uma_zcreate', start_file='sys/vm/uma_core.c')`.

---

## Multi-pass review pipeline

```mermaid
flowchart TD
    Start([chapter from chapters.yaml]) --> Draft

    Draft["**Step 1 — Draft** (writer agent, 25 steps)<br/>build_chapter_prompt → full markdown draft<br/>tools: read_freebsd_source, search_books,<br/>explore_tree, resolve_c_definition"]
    Draft --> Review

    Review{"**Step 2 — Review** (reviewer agent, 10 steps)<br/>build_review_prompt → JSON verdict<br/>tools: search_books only<br/>does NOT edit the draft"}
    Review -- "grade=PASS AND<br/>no issues AND<br/>no FAIL criteria" --> FactCheck
    Review -- "any FAIL<br/>(and revisions left)" --> Revise
    Review -- "max_revisions reached<br/>or JSON unparseable twice" --> Unapproved[mark UNVERIFIED]
    Unapproved --> FactCheck

    Revise["**Step 3 — Revise** (writer agent)<br/>build_revision_prompt(chapter, draft, review_raw)<br/>can ADD or REMOVE content<br/>→ new full draft"]
    Revise --> Review

    FactCheck{"**Step 4 — Fact-check** (deterministic)<br/>fact_check_draft: paths + structs + functions<br/>verified against FreeBSD source tree<br/>does NOT edit the draft"}
    FactCheck -- "all claims verified" --> Write
    FactCheck -- "missing paths /<br/>structs / funcs" --> FactFix

    FactFix["**Step 5 — Fact-fix** (writer agent)<br/>_build_fact_check_prompt(chapter, draft, facts)<br/>corrects bad paths, replaces missing symbols,<br/>removes unfixable claims"]
    FactFix --> Write

    Write["**Step 6 — Atomic write**<br/>rename existing output → .bak<br/>_atomic_write (tempfile + fsync + os.replace)<br/>delete .bak on success / restore on failure"]
    Write --> End([chapter file in FreeBSD src tree])

    classDef writer fill:#dbeafe,stroke:#1e40af,color:#1e3a8a;
    classDef reviewer fill:#fef3c7,stroke:#92400e,color:#78350f;
    classDef deterministic fill:#dcfce7,stroke:#166534,color:#14532d;
    classDef io fill:#f3f4f6,stroke:#374151,color:#111827;
    class Draft,Revise,FactFix writer;
    class Review reviewer;
    class FactCheck,Write deterministic;
    class Start,End,Unapproved io;
```

Blue = writer agent (produces text). Yellow = reviewer agent (verdict only,
never edits). Green = deterministic logic (no LLM). Grey = I/O boundaries.

### Step-by-step roles

Two agents, two distinct roles. **The reviewer never edits the draft text** —
it only emits a JSON verdict. Every textual change (including additions) is
produced by the writer in a follow-up call. This separation is why each step's
prompt can stay small and focused.

1. **Draft (writer).** Reads `build_chapter_prompt(chapter)` — focus,
   `scope_guard`, sections, key questions, mandatory output template, and the
   existing target file as read-only context. Has full tool access:
   `read_freebsd_source`, `search_books`, `explore_tree`,
   `resolve_c_definition`. Produces a complete markdown draft, free to add
   anything within the template.

2. **Review (reviewer, looped).** Reads `build_review_prompt(chapter, draft)`.
   Tools: `search_books` only — the rubric is evaluated against the draft, not
   against the tree. Emits JSON: `grade`, `issues[]`, `praise[]`, and
   per-criterion stamps (`PASS` / `FAIL: <reason>`). **Does not modify the
   draft.** JSON parse failures get one retry before the chapter is marked
   unapproved.

3. **Revise (writer).** Only runs when the review gate fails. Reads
   `build_revision_prompt(chapter, draft, review_raw)` — original chapter
   prompt + the rejected draft + the reviewer's raw JSON. The writer can both
   **remove** content (trim out-of-scope material the reviewer flagged) AND
   **add** content (fill a section the reviewer marked thin, answer a missed
   key question). Loop returns to Step 2 with the new draft.

4. **Fact-check (deterministic, no agent).** `fact_check_draft` extracts every
   file path, struct name, and function name claimed in the draft and verifies
   each against the FreeBSD source tree (3-stage grep pipeline in
   `_batched_grep_present`, plus `_resolve_path_in_tree`). Returns a dict of
   missing/corrected items. **Does not modify the draft.**

5. **Fact-fix (writer).** Only runs when fact-check finds issues. Reads
   `_build_fact_check_prompt(chapter, draft, facts)` — original chapter
   context + current draft + the specific bad claims. The writer corrects bad
   paths, replaces missing structs with verified ones, and removes unfixable
   claims. Same writer agent as Step 1 — has full source access via tools.

6. **Atomic write.** Existing output is renamed to `.bak`, the new draft is
   atomically replaced into place via `_atomic_write` (tempfile + fsync +
   `os.replace`), and the backup is deleted on success / restored on failure.
   If review didn't approve or fact-fix crashed, an `UNVERIFIED DRAFT` banner
   is inserted under the H1.

### Reviewer rubric

The reviewer grades on 7 criteria:
1. **Completeness** — all key questions answered
2. **Accuracy** — no hallucinated structs/functions/paths
3. **Source Coverage** — expected files actually discussed (not just listed)
4. **Mermaid Diagram** — valid syntax, meaningful content
5. **Accessibility** — explains WHY, not just WHAT
6. **Structure** — every section the chapter declared in `sections:` is present and substantive
7. **No marketing language** — no "comprehensive", "robust", "seamless", "leverage", "elegant", etc. The reviewer quotes the offending sentence.

The strict gate (in `_review_passes`) only approves a chapter when `grade == "PASS"` AND `issues[]` is empty AND every criterion passes. This prevents the failure mode where the model returns `grade=PASS` while individual criteria still say `FAIL`.

Default: `--max-revisions 2` (one draft + up to two revision rounds).

---

## Chapter definitions (`chapters.yaml`)

Each chapter has:
- `title` — chapter heading
- `output_file` — where to write (relative to FreeBSD src root)
- `source_dirs` — directories the agent should explore
- `source_files` — specific files the agent should examine
- `focus` — what aspect to emphasize
- `key_questions` — questions the chapter must answer
- `mermaid` — diagram type: `sequence`, `flowchart`, `class`, or `state`
- `sections` — *(optional)* which template sections this chapter should produce. Defaults to the full set: `Quick Summary`, `Architecture`, `Key Data Structures`, `Deep Dive`, `Flow / Diagram`, `Advanced Notes`, `Comparison`, `See Also`. A tree-overview chapter, for example, can drop `Key Data Structures` and `Deep Dive` because there's no specific subsystem to feature. The catalog of valid section names lives in `_SECTION_CATALOG` in `generate-doc.py`.
- `scope_guard` — *(optional)* free-text hard rule injected into the writer prompt under `## Scope Guard`. Use this when section selection alone isn't enough to keep the writer on-topic. The tree-overview chapter uses it to forbid pulling subsystem internals (vm_page, struct proc, etc.) from referenced source directories.

Current chapters (13):
1. FreeBSD Source Tree Overview
2. The FreeBSD Kernel — Structure and Entry Point
3. UEFI Bootloader-to-Kernel Handoff
4. Virtual Memory Subsystem
5. Process Management and Scheduling
6. The Buffer Cache and I/O Subsystem
7. Virtual File System (VFS) Layer
8. UFS Filesystem Implementation
9. Network Stack Architecture
10. Device Driver Framework
11. Interrupt Handling
12. Jails and System Isolation
13. The FreeBSD Build System

---

## Output

Each chapter produces a markdown file in the FreeBSD source tree:

| Directory | File |
|---|---|
| root | `README_internals.md` |
| `sys/` | `README.md` |
| `sys/vm/` | `README.md`, `README_bcache.md` |
| `sys/kern/` | `README_process.md`, `README_driver.md`, `README_intr.md`, `README_jail.md` |
| `sys/fs/` | `README.md` |
| `sys/ufs/` | `README.md` |
| `sys/net/` | `README.md` |
| `stand/efi/loader/` | `README.md` |
| `share/mk/` | `README.md` |

The root file is `README_internals.md` (not `README.md`) to avoid overwriting the upstream FreeBSD `README.md`, which is itself a source for that chapter.

Each chapter follows a section template. The full template — which a chapter gets by default — is:

```markdown
# {Chapter Title}

## Quick Summary
(3-4 paragraphs: what this subsystem does and why it matters.
No code — accessible to any reader who knows C.)

## Architecture
(technical explanation with source file references)

## Key Data Structures
(C structs with field explanations, referencing header files)

## Deep Dive
(Source code walkthrough: trace through key functions step-by-step
with code snippets. Intermediate reading level.)

## Flow / Diagram
(Mermaid diagram — sequence, flowchart, class, or state)

## Advanced Notes
(Practical insights for advanced readers: debugging with DTrace,
performance implications, race conditions, common pitfalls,
connection to OS theory.)

## Comparison
(How Linux/macOS/NetBSD implement the same concept — structural
differences, not code details. 2-4 paragraphs.)

## See Also
(related chapters and source directories)
```

A chapter can opt out of sections that don't fit by declaring `sections:` in `chapters.yaml`. Chapter 1 (the tree overview) drops `Key Data Structures`, `Deep Dive`, and `Comparison` for that reason — there are no single structs or functions worth featuring at the tree-level view, and forcing those sections led the writer to invent thin, out-of-context examples. A chapter can also add a `scope_guard:` block to forbid specific patterns the writer would otherwise drift into.

Rules:
- Always reference specific file paths (e.g., `sys/vm/vm_page.c`)
- Include C code snippets where they illuminate the design
- Quick Summary has no code; Deep Dive has code snippets; Advanced Notes covers debugging, performance, and pitfalls
- **No marketing language** — words like *comprehensive*, *robust*, *seamless*, *leverage*, *elegant*, *powerful*, *modern* are forbidden in the writer prompt and flagged by the reviewer.

Every generated file ends with a **provenance footer** recording the LLM model id, endpoint, and UTC timestamp used for that run — so a reader can trace which model wrote which document.

---

## File layout

```
DaemonDocs/
├── README.md              ← you are here
├── generate-doc.py        ← single self-contained script
├── chapters.yaml          ← 13 chapter definitions
├── requirements.txt       ← Python dependencies
└── .index/                ← cached corpus + TF-IDF index (gitignored)
    ├── books_corpus.txt
    ├── book_hashes.json
    ├── tfidf_index_matrix.npy
    └── tfidf_index_meta.json
```

---

## Constraints

- **FreeBSD 16-CURRENT** — use only pure-Python packages (`smolagents`, `openai`, `PyPDF2`, `pyyaml`, `numpy`)
- **Single file** — all logic in `generate-doc.py`, no separate modules
- **LLM is local** — llama-server on port 8080, not an external API
- **CHM books** require `hhextract` (hh suite) — graceful fallback with warning
- **Output files** use `README_*` suffix to not overwrite existing FreeBSD README files

