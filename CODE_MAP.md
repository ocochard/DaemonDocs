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
   `ExploreTree`, `ResolveCDefinition`. The fixed tool set the
   writer agent runs against.
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
`build_navigation`, `_add_see_also_links` — and
**Chapter index** — `build_chapter_index`,
`_extract_glossary_terms`. These run once at the end of
`main()`.)

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
| Add a reviewer rubric criterion | `build_review_prompt` | Add to the numbered rubric AND to the JSON output schema (`"criteria": { ... }`) AND to the gate. The gate is `_review_passes`, which iterates `criteria.values()` — adding a key there is automatic. The fail-counter `_criteria_fail_count` hard-codes the count `7` for the "broken JSON" worst-case; bump it if you add criteria. |
| Inject ground truth into the reviewer | `build_review_prompt` | Pre-validate before the prompt is rendered, build a `verified_FOO_block` string, interpolate it after `{verified_block}`. The Accuracy criterion and the **No hedges** block reference these blocks by name — update both when adding a new ground-truth category. |
| Add a new section type (output template) | `_SECTION_CATALOG` | Each entry has `template_body`, `rubric_body`. Per-chapter section list comes from `_chapter_sections(chapter)` reading `sections:` in `chapters.yaml`. |
| Add a new fact-check verifier | Banner **Fact-checking** | Pair: `_extract_FOO(text) -> List` and `_verify_FOO(items, src_root) -> List[missing]`. Use `_verify_with_cache` if the verifier is a grep over `sys/` (free re-runs via `_FACT_CHECK_CACHE`). Wire into `fact_check_draft` and `_build_fact_check_prompt`. Existing examples: structs, functions, kernel options, DTrace probes. |
| Change sampler params | `create_writer_agent` / `create_reviewer_agent` | Pass kwargs to `OpenAIServerModel(...)`. They land on every API call via `self.kwargs` in smolagents' `Model._prepare_completion_kwargs`. |
| Add a new chapter | `chapters.yaml` only | Schema is documented in the YAML's header comment. `output_file` must NOT collide with an upstream-shipped FreeBSD file; if it does, use a sibling name (see chapter 1's `README_internals.md` rationale). |
| Add a writer tool | Banner **Smolagents tools** + `create_writer_agent` `tools=[...]` | `additional_authorized_imports` is **deliberately minimal** (`re`, `json`). Do not add `os` or `pathlib` — see the comment in `create_writer_agent` for why. |

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

---

## Execution topology — where the script runs

`generate-doc.py` ALWAYS runs from `framework` (fw1, the primary
host). That host owns the repo, the FreeBSD source tree, the books
corpus, and one llama-server. `framework2` (192.168.100.136) is
ONLY an additional llama-server endpoint — no repo, no source
tree, no script. Parallelism is two `generate-doc.py` processes
*on fw1*, each pointed at a different `OPENAI_BASE_URL` (one
local, one fw2). The hostname `framework2` does not resolve from
fw1; use the IP from `~/.ssh/config`.

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
