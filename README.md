# FreeBSD Internals

[![GitHub](https://img.shields.io/badge/GitHub-DaemonDocs-181717?logo=github)](https://github.com/ocochard/DaemonDocs)

AI-generated documentation of FreeBSD internals. Each chapter produces a `README.md` placed directly in the relevant FreeBSD source directory — so anyone can `git clone` the tree and find educational material right next to the code.

## Vision

Build documentation of FreeBSD internals by having AI agents study the FreeBSD source code and cross-reference it with FreeBSD books. The output is a set of `README.md` files placed throughout the FreeBSD source tree — so any reader can `git clone` the tree and find educational material right next to the code.

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
   - A reviewer agent grades the draft on 6 criteria
   - If needed, the writer revises — up to `--max-revisions` rounds
5. **Writes** the final `README.md` into the relevant source directory

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

```
writer agent (25 steps, full tools)
    ↓ draft markdown
reviewer agent (10 steps, book search only)
    ↓ JSON: { grade, criteria, issues, praise }
    ├─ PASS → write file
    └─ NEEDS_REVISION → writer agent revises
                            ↓ revised draft
                        reviewer agent re-evaluates
                            ↓
                    loop until PASS or max_revisions reached
```

The reviewer grades on 6 criteria:
1. **Completeness** — all key questions answered
2. **Accuracy** — no hallucinated structs/functions/paths
3. **Source Coverage** — expected files actually discussed (not just listed)
4. **Mermaid Diagram** — valid syntax, meaningful content
5. **Accessibility** — explains WHY, not just WHAT
6. **Structure** — all 9 required sections present

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

Each chapter produces a `README.md` in the FreeBSD source tree:

| Directory | File |
|---|---|
| root | `README.md` |
| `sys/` | `README.md` |
| `sys/vm/` | `README.md`, `README_bcache.md` |
| `sys/kern/` | `README_process.md`, `README_driver.md`, `README_intr.md`, `README_jail.md` |
| `sys/fs/` | `README.md` |
| `sys/ufs/` | `README.md` |
| `sys/net/` | `README.md` |
| `stand/efi/loader/` | `README.md` |
| `share/mk/` | `README.md` |

Each file follows a strict 9-section template:

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

Rules:
- Always reference specific file paths (e.g., `sys/vm/vm_page.c`)
- Include C code snippets where they illuminate the design
- Quick Summary has no code; Deep Dive has code snippets; Advanced Notes covers debugging, performance, and pitfalls

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

---

<details>
<summary><strong>Implemented features</strong></summary>

- PDF extraction from books (CHM requires `hhextract`)
- TF-IDF semantic search index with paragraph/section-aware chunking
- Four agent tools: read source, search books, explore tree, resolve C definitions
- Multi-pass writer → reviewer → revise pipeline
- Incremental runs (skip unchanged books, skip existing output)
- Corpus: books, man9, Handbook, git logs, papers, kerneldoc
- Source tree awareness (reads existing docs before writing)
- Structured fact-checking (verifies claimed structs / functions / paths)
- Cross-README navigation links and cross-chapter reference index
- Progressive difficulty (Quick Summary / Deep Dive / Advanced Notes)
- OS comparisons (Linux / macOS / NetBSD)
- Flags: `--dry-run`, `--force`, `--reindex`, `--chapter N`, `--max-revisions N`, `--nav-only`, `--index`

</details>
