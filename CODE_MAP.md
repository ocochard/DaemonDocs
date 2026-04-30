# DaemonDocs — Code Map

A navigation guide to `generate-doc.py` for anyone (human or LLM)
about to edit it. Optimized for "where do I add X?" rather than
"what does X do?" — read the docstrings for the latter.

**Anchors are function/class names and the `# ---` section banners
inside `generate-doc.py`.** Line numbers are deliberately omitted —
they drift. Grep for the anchor.

The repo has only one nontrivial code file. `chapters.yaml` is data,
`README.md` is the project pitch, `FUTURE_IMPROVEMENTS.md` is the
backlog (and includes pipeline-failure post-mortems worth reading
before changing pipeline behavior).

---

## File layout — the eight banner sections of `generate-doc.py`

The file is divided by `# ---` banner comments. In source order:

1. **Configuration** — `SRC_ROOT`, `BOOKS_DIR`, `MODEL_CONFIG`,
   `RESOLVED_PROVENANCE`. Env vars: `FREEBSD_SRC`, `BOOKS_DIR`,
   `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`.
2. **Book corpus extraction** — PDF/CHM/EPUB → text; FreeBSD
   handbook + man pages + git logs. Builds the searchable corpus.
3. **TF-IDF index** — `class TfidfIndex`, `get_or_build_index`.
   Persistent on-disk index used by the `search_books` tool.
4. **Smolagents tools** — `ReadFreeBSDSource`, `SearchBooks`,
   `ExploreTree`, `DirectoryMap`, `ResolveCDefinition`. The fixed
   tool set the writer agent runs against. `DirectoryMap` returns
   a structured one-level summary (subdirs + Makefile SRCS + per-
   file struct/function names + top-of-file comments) so the
   writer can orient before reading individual files.
5. **Prompt builders** — `build_chapter_prompt` (writer),
   `build_review_prompt` (reviewer), `build_revision_prompt`
   (writer revising). Plus `_SECTION_CATALOG`, `_chapter_sections`,
   and `gather_source_context`.
6. **Agent runtime** — `create_writer_agent`, `create_reviewer_agent`,
   `_run_agent`, `_looks_like_stub`. This is where sampler params
   are pinned on `OpenAIServerModel`.
7. **Orchestrator** — `run_chapter` (the pipeline),
   `_review_passes` (the gate), `load_chapters`, `_extract_json`.
8. **Fact-checking** — symbol/path extractors, verifiers, the
   `_FACT_CHECK_CACHE`, `fact_check_draft`,
   `_build_fact_check_prompt`.

(Two more banner sections follow that aren't part of the
per-chapter pipeline: **Cross-README navigation** —
`build_navigation`, `_add_see_also_links`,
`_build_chapter_rels` — and **Chapter index** —
`build_chapter_index`, `_extract_glossary_terms`. These run
once at the end of `main()`. Chapter relationships are
derived from `chapters.yaml` (`related:` per chapter, or
`family:`-mates as fallback) — never hand-maintained in
the script.)

---

## The pipeline — read this before touching `run_chapter`

`run_chapter` orchestrates one chapter. Its docstring has the full
step-by-step contract; here is the shape:

```
DRAFT (writer)         build_chapter_prompt → CodeAgent → markdown
   ↓
REVIEW LOOP            build_review_prompt → reviewer → JSON verdict
   ↓ (if FAIL)
REVISE (writer)        build_revision_prompt → CodeAgent → markdown
   ↓ (back to REVIEW, up to max_revisions)
FACT-CHECK             fact_check_draft (deterministic, grep-based)
   ↓ (if issues)
FACT-FIX (writer)      _build_fact_check_prompt → CodeAgent → markdown
   ↓
ATOMIC WRITE           rename existing → .bak, write new, drop .bak
```

**Best-draft tracking:** `run_chapter` keeps the lowest-fail-count
draft seen so far and writes *that* on exit, even if the final
revision regressed. Don't undo this — see FUTURE_IMPROVEMENTS.md
"Revisions can regress."

**Stub detection:** `_looks_like_stub` flags drafts that look like
status strings (`"README.md successfully written"`) instead of
chapter content. Initial-draft stubs get one retry; revision /
fact-fix stubs abort that step and roll back to the best draft.

**The reviewer is a `CodeAgent` with only `search_books`** — no
source-tree access. It cannot grep, cannot read files. Anything
the reviewer needs to know about the source tree must be
*injected into its prompt* by `build_review_prompt`. This is the
shape of the path-validation and symbol-validation blocks already
in place; new "ground truth" injections should follow the same
pattern.

---

## Where to add X

| Want to... | Edit | Pattern to follow |
|---|---|---|
| Add a writer rule (e.g. forbid a phrasing) | `build_chapter_prompt` | The existing **Rules:** block, then the **Quote, don't paraphrase** block. Each rule names the failure mode + names the right instinct + gives a concrete example. |
| Add a reviewer rubric criterion | `build_review_prompt` | Add to the numbered rubric AND to the JSON output schema (`"criteria": { ... }`) AND to the gate. The gate is `_review_passes`, which iterates `criteria.values()` — adding a key there is automatic. The fail-counter `_criteria_fail_count` hard-codes the current count (`9`) for the "broken JSON" worst-case; bump it if you add criteria. Also bump the `N - fail_count`/`/N` print formatting in `run_chapter`'s review loop and rollback log, and the `best_fails = N+1` initializer (one more than the max possible). If the criterion is conditional (only fires when a certain section is present), gate it on `"Section Name" in sections` and emit a `PASS: not required` JSON line in the off branch — see the `wants_diagram` (Mermaid) and `wants_comparison` (Comparison Quality) gates as templates. |
| Inject ground truth into the reviewer | `build_review_prompt` | Pre-validate before the prompt is rendered, build a `verified_FOO_block` string, interpolate it alongside the existing `verified_block` / `symbol_block` / `macro_block`. The Accuracy criterion and the **No hedges** block reference these blocks by name — update both when adding a new ground-truth category. |
| Inject ground truth into the writer | `_build_symbol_catalog` (called by `build_chapter_prompt`) | Pre-extract real symbol names from the chapter's `source_files` + bounded `source_dirs` sample, render an `## Authoritative Symbol Catalog` block. Caps live in `_CATALOG_MAX_*` and `_CATALOG_FILES_PER_DIR` — keep them tight; this is prompt-cost, not fact-check-cost. To add a new symbol kind, extend `_dirmap_extract_names` (which is shared with `directory_map`). |
| Add a new section type (output template) | `_SECTION_CATALOG` | Each entry has `template_body`, `rubric_body`. Per-chapter section list comes from `_chapter_sections(chapter)` reading `sections:` in `chapters.yaml`. |
| Add a new fact-check verifier | Banner **Fact-checking** | Pair: `_extract_FOO(text) -> List` and `_verify_FOO(items, src_root) -> List[missing]`. Use `_verify_with_cache` if the verifier is a grep over `sys/` (free re-runs via `_FACT_CHECK_CACHE`). Wire into `fact_check_draft` and `_build_fact_check_prompt`. Existing examples: structs, functions, kernel options, DTrace probes, MALLOC_DEFINE tags. |
| Change sampler params | `create_writer_agent` / `create_reviewer_agent` | Pass kwargs to `OpenAIServerModel(...)`. They land on every API call via `self.kwargs` in smolagents' `Model._prepare_completion_kwargs`. |
| Add a new chapter | `chapters.yaml` only | Schema is documented in the YAML's header comment. `output_file` must NOT collide with an upstream-shipped FreeBSD file; if it does, use a sibling name (see chapter 1's `README_internals.md` rationale). Set `family:` (one of the 7 tags) so the new chapter shows up in the right family-mate "Related" sidebars by default; add `related:` only if family-mates aren't right. |
| Change which chapters appear as "Related" in a sidebar | `chapters.yaml` (`related:` field) | The chapter relationship map is derived from `chapters.yaml` by `_build_chapter_rels` — `related:` wins, family-mates are the fallback. Do NOT reintroduce a hand-maintained `CHAPTER_RELS` dict in `generate-doc.py` — that duplication was the bug fixed by this design (rename in YAML, dict goes silently stale). |
| Add a writer tool | Banner **Smolagents tools** + `create_writer_agent` `tools=[...]` | `additional_authorized_imports` is **deliberately minimal** (`re`, `json`). Do not add `os` or `pathlib` — see the comment in `create_writer_agent` for why. Also add a regex to `_STATS_TOOL_PATTERNS` so the per-chapter tool-use banner counts the new tool. |
| Track a new tool-use metric in the per-chapter banner | `_collect_tool_stats` / `_format_stats_banner` | `_run_agent` calls `_collect_tool_stats` after every agent.run; `run_chapter` accumulates via `_merge_tool_stats` and prints the banner before returning. CodeAgent's tool calls live in `agent.memory.steps[i].code_action` (Python source), not in `tool_calls`, so the helpers regex over `code_action` text. Memory resets on each `agent.run(reset=True)` so collection MUST happen inside `_run_agent` before the next stage runs. |

---

## Things that look weird until you read the comment next to them

- **`output_file: README_internals.md` for chapter 1** — upstream
  FreeBSD ships `README.md` at the tree root. Writing there would
  clobber it and cause a read-vs-write collision in `run_chapter`.
- **`additional_authorized_imports=["re", "json"]`** in
  `create_writer_agent` — keeps the sandbox honest. Authorizing
  `os`/`pathlib` lets the model bypass the "no file I/O" prompt
  rule and emit status strings instead of chapter content.
- **Reviewer's `criteria` dict is the gate, not `grade` or
  `issues`** — see `_review_passes` docstring. `grade` and
  `issues` are intentionally ignored; real defects must surface
  as a FAIL criterion.
- **`_strip_comparison_section` runs before symbol extraction** —
  the `## Comparison` section legitimately mentions
  Linux/macOS/NetBSD names which would otherwise be flagged as
  hallucinated.
- **`_extract_function_names` requires `()` evidence** — bare
  backticked identifiers are skipped because they're dominated
  by struct fields, type names, sysctls, and parameter names.
  Grepping all of those wastes fact-fix steps.
- **`_FACT_CHECK_CACHE` is process-global, keyed by
  `(kind, src_root, symbol)`** — same symbol verified once per
  run regardless of which chapter or revision round asked for it.
  This is why pre-validating symbols in `build_review_prompt` is
  effectively free for the post-review fact-check.
- **`subprocess.run(..., text=True, errors='replace', ...)`** in
  every grep-over-tree site — FreeBSD source contains a few non-UTF8
  bytes (Latin-1 author names in old driver comments). Without the
  error policy, Python's text-mode decode raises `UnicodeDecodeError`
  mid-pipeline and aborts the whole run before the atomic write —
  ch14 (sys/net) hit this on 2026-04-30. Any new
  `subprocess.run(..., text=True, ...)` that reads from `sys/`
  needs `errors='replace'` for the same reason.
- **Tool-use stats are extracted from `step.code_action`, not
  `step.tool_calls`** — for `CodeAgent`, `tool_calls` records the
  outer `python_interpreter` call once per step; the *real* tool
  invocations (`read_freebsd_source(...)`, etc.) live as Python
  source inside `code_action`. `_collect_tool_stats` regexes the
  source text. Also: `agent.run(reset=True)` (the default) wipes
  `agent.memory` at the start of every call, so stats MUST be
  collected inside `_run_agent` before the next stage runs.

---

## Execution topology — where the script runs

`generate-doc.py` runs on a single primary host that owns the repo,
the FreeBSD source tree (`$FREEBSD_SRC`), the books corpus
(`$BOOKS_DIR`), and at least one llama-server endpoint. Additional
llama-server endpoints (separate hosts on the LAN) are reachable via
their `OPENAI_BASE_URL` and used purely as compute — they do NOT need
the repo, the source tree, or a copy of the script. Parallelism is N
`generate-doc.py` processes on the primary host, each pointed at a
different `OPENAI_BASE_URL`. Resolve remote endpoint hosts by IP or
via `~/.ssh/config` aliases as appropriate; the script itself only
cares about the `OPENAI_BASE_URL` env var.

---

## Don't change without reading the rationale

- `_review_passes` (criteria-only gate)
- `additional_authorized_imports` in `create_writer_agent`
- `output_file: README_internals.md` for chapter 1
- The `# Indentation gymnastics` comment in `build_chapter_prompt`
- `_GREP_TIMEOUT_SEC` and the two-stage grep in `_batched_grep_present`
- The "no marketing language" forbidden-words list in
  `build_chapter_prompt` and `build_review_prompt` — they MUST
  stay in sync. The reviewer's list is the ground truth; the
  writer's list is the warning.

Each of these has a comment explaining why it is the way it is.
If a change seems obviously better, the comment exists because the
obvious thing was tried and broke something specific.
