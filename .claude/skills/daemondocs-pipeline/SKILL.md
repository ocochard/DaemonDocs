---
name: daemondocs-pipeline
description: Work on the DaemonDocs FreeBSD documentation generator (generate-doc.py). Covers the smolagents writer/reviewer pipeline, fact-checking grep tricks, and project-specific gotchas. Use when editing generate-doc.py, chapters.yaml, or running the pipeline.
---

# DaemonDocs pipeline knowledge

DaemonDocs is an AI-driven generator that produces FreeBSD-internals chapters using
smolagents + a local llama-server. Top-level goal: **generate documentation with the
fewest errors possible.** Bias every change toward correctness over throughput.

## Execution layout

- Repo lives at `/usr/home/olivier/DaemonDocs/` (development).
- Heavy runs happen on host `framework` (`ssh framework`, `scp` for sync — no rsync).
  This host has the FreeBSD source tree at `~/freebsd-src/`, books at `~/books/`,
  the index at `~/DaemonDocs/.index/`, and the llama-server log at
  `/tmp/llama-server.log`. Never name `framework` in any generated documentation.
- Workflow when iterating: edit locally → `scp generate-doc.py framework:DaemonDocs/`
  → `ssh framework "cd DaemonDocs && python3 generate-doc.py --chapter N [--force]"`.

## Pipeline shape

Per chapter, `run_chapter()` does: draft → review/revise loop (strict gate) →
fact-check → atomic write. Two agents:
- **Writer** (`create_writer_agent`) — has tools `ReadFreeBSDSource`,
  `SearchBooks`, `ExploreTree`, `ResolveCDefinition`.
- **Reviewer** — graded JSON, must satisfy a strict gate to approve.

Strict review gate (in `_review_passes`): requires `grade == "PASS"` AND empty
`issues[]` AND every criterion not stamped `FAIL:`. Don't loosen this — the
reviewer often returns `grade=PASS` while individual criteria say FAIL, and
silently approving those degrades output quality.

## Hard-won gotchas

### 1. smolagents `final_answer()` returns the raw object

If the agent calls `final_answer({...})`, `agent.run()` returns a dict, not a
string. Every downstream consumer (`_extract_json`, `.strip()`, `_atomic_write`)
expects a string. The fix is in `_run_agent` — it coerces dict/list returns via
`json.dumps`. **Never call `.strip()` directly on the return of `_run_agent`
without going through that coercion.**

### 2. smolagents nullable tool args need `nullable: True`

If a tool's `forward(...)` has an optional argument with a default, its entry in
the tool's `inputs` dict MUST set `"nullable": True`, or you get
`AssertionError: Nullable argument 'X' should have key 'nullable' set to True`.
See `ResolveCDefinition.start_file` for the canonical example.

### 3. Don't write into upstream FreeBSD source files

Chapter 1's output is `README_internals.md`, NOT `README.md` — the FreeBSD tree
already ships its own `README.md` and we read it as a *source* for chapter 1.
Writing the chapter to `README.md` clobbers upstream and creates a
read-vs-write collision in `run_chapter()`. When adding new chapters in
`chapters.yaml`, check that `output_file` does not collide with any path in
`source_files`, and does not match a real upstream file.

### 4. Backup rename is deferred until just before atomic_write

`run_chapter()` used to rename `output_path → .bak` up front, which broke any
chapter where `output_file` overlapped a `source_files` entry (the writer
couldn't read its own source). The rename is now deferred until immediately
before `_atomic_write`. Keep that ordering — don't hoist it back to the top of
the function.

### 5. BSD `grep -E` chokes on nested alternations

On FreeBSD, `grep -E '(a|b|c|d|...)' big-tree/` with many alternations runs 40s+
on `sys/`. The fact-check pipeline avoids this with a 3-stage approach in
`_batched_grep_present()`:

1. `grep -rhwF` (fixed-string, multiple `-e` patterns) — extracts every line
   containing any candidate symbol. Very fast.
2. `grep -E shape_grep` — narrows to lines that *look like* a struct or
   function definition (`^struct ID *\{` / `ID *\*? *ID(`). Coarse but cheap.
3. Python `re.search(pattern_template)` per symbol — exact match against the
   whitelisted lines. Surgical.

Output is bounded with `head -c 1048576` to keep the buffer sane. Do NOT replace
this with a single-pass `grep -E` — the pathology is real.

Also: `\s` is slow in BSD `grep`; use a literal space when you can.

### 6. `_find_md_header` uses `[ ]{0,3}`, NOT `\s{0,3}`

Plain `\s` matches newlines. Combined with `^` in multiline mode, the match
slides to the newline *before* the header, breaking header-line bounds. Use
`[ ]{0,3}` (literal space class). Same applies to any markdown header parser
you add.

### 7. `.s` truncation in path extraction

`_extract_file_paths` uses an `(?!\w)` lookahead at the end of every recognised
extension so `.s` doesn't bite into `.sh`. Don't simplify the regex back to
greedy alternation — `gnu/usr.bin/gdb/gdb-add-index.sh` will get truncated to
`.s`.

### 8. TF-IDF index staleness

`get_or_build_index()` compares mtimes of corpus vs. saved matrix. If you mutate
the corpus (add/remove books, prune deleted), the next run will rebuild even
without `--force`. If results look stale, check `~/DaemonDocs/.index/` mtimes.

### 9. McKusick "Design and Implementation of 4.4BSD" content

Already covered by the freebsd-doc git clone at
`documentation/content/en/books/design-44bsd/_index.adoc`. **Don't add the CHM
to `~/books/` — it's redundant.** Phase 1b ingests this automatically.

### 10. Per-chapter section list (chapters.yaml `sections:`)

Chapters can declare which template sections they want via a `sections:` list.
Default is the full 8-H2-section template (`_DEFAULT_SECTIONS` in
generate-doc.py). The catalog of valid section names lives in
`_SECTION_CATALOG`. Only add to it; don't fork section bodies inside
chapter prompts.

Use this to drop sections that don't make sense for a chapter — e.g. the
tree-overview chapter has no specific structs to feature, so it omits
`Key Data Structures` and `Deep Dive`. Forcing those sections led the model
to invent thin, out-of-context examples (a pattern flagged by the user).

The reviewer rubric and `_extract_overview` both derive their section sets
from `_chapter_sections()` and `_SECTION_CATALOG` respectively, so adding a
section to the catalog auto-extends both.

### 11. Per-chapter `scope_guard` (chapters.yaml)

A chapter can declare a `scope_guard:` block — multi-line free text that gets
injected into the writer prompt as a hard rule between `## Focus` and
`## Instructions`. Use it when section selection alone isn't enough to keep
the writer on-topic.

The tree-overview chapter, for example, even with `Key Data Structures` and
`Deep Dive` removed, will still pull `vm_page` / `vm_pagequeue` snippets from
sys/vm into the Architecture section because the writer treats every
referenced directory as fair game. The scope_guard explicitly forbids this:
"give one-sentence directory-purpose only and refer the reader to the
relevant chapter."

Implementation: `build_chapter_prompt` reads `scope_guard = chapter.get(...)`,
applies the same partition+indent indentation gymnastics as the section
template body, and wraps it in `## Scope Guard\n(HARD RULE — ...)`. Empty
guard becomes an empty string and produces a clean blank line — verified
with the no-guard rendering test.

### 12. No marketing language

Both writer prompt and reviewer rubric forbid corporate-blog adjectives:
*comprehensive, robust, seamless, leverage, cutting-edge, elegant, powerful,
simply, modern, sophisticated*, etc. Reviewer rubric criterion 7
(`no_marketing`) catches violations and quotes the offending sentence.
The criteria count is now 7, not 6 — `_criteria_fail_count()` and the
log-line in `run_chapter()` both reflect that. If you add another rubric
criterion, update both.

### 13. Strict reviewer rubric (no exceptions)

`build_review_prompt` enforces: `grade=PASS` only when *every* criterion is
PASS AND `issues` is empty. Any FAIL criterion or any non-empty `issues` →
`grade` MUST be `NEEDS_REVISION`. Don't loosen this back to "3 or fewer
FAILs → PASS" — the model previously returned `PASS` while listing 4
issues, and the strict gate (`_review_passes`) silently rejected those
drafts but the writer never saw the rubric had been violated.

### 14. Fact-checker noise filter (`_FACT_CHECK_IGNORE`, `_MACRO_PREFIX_RE`)

`_extract_struct_names` and `_extract_function_names` call
`_filter_known_noise` to drop:

- `_FACT_CHECK_IGNORE` frozenset — make targets (buildworld, universe…),
  config knobs (GENERIC, OBJTOP…), filenames (Makefile, UPDATING),
  parameter names from common signatures (init, func, order, udata, sid),
  globals frequently mentioned but not callable (thread0, proc0, btext),
  and Linux/macOS structs/funcs that legitimately appear in `## Comparison`
  sections (vm_area_struct, start_kernel, kernel_bootstrap…).
- `_MACRO_PREFIX_RE` — well-known C macro families: `SI_SUB_*`,
  `SI_ORDER_*`, `SDT_PROBE_*`, `TAILQ_*`, `LIST_*`, `KASSERT*`, `VOP_*`,
  etc. These are `#define`d, not callable, and grepping for them as
  function-shape misses every time.
- `WITH_*`, `WITHOUT_*`, `MK_*`, `NO_*` ALL_CAPS make/kernel knobs.

Also: `_extract_function_names` requires *call evidence* — it only
matches backticks-with-parens (\`foo()\` or \`foo(args)\`) and prose
patterns ("the foo() function", "calls foo()"). Bare backticked
identifiers (\`foo\`) are NOT treated as function candidates because the
population is dominated by struct fields, type names, sysctls, and
parameter names — every one of which generates a fact-fix step.

`_strip_comparison_section` removes `## Comparison` H2 sections from the
fact-check input so cross-OS symbols are never grepped against the
FreeBSD tree. The reviewer still sees the full draft; only the
fact-checker's input is filtered.

### 15. Hallucination check for kernel options + DTrace probes

`fact_check_draft` extracts and verifies two more categories:

- **Kernel options** — `_extract_kernel_options` picks up `option FOO`
  prose and backticked patterns matching VERBOSE_*/DEBUG_*/INVARIANT*/
  WITNESS*/KTR*. `_verify_kernel_options` greps `sys/conf/options*` and
  `sys/conf/NOTES` for each name. Catches fabricated knobs like
  `VERBOSE_SYSINIT`.
- **DTrace probes** — `_extract_dtrace_probes` parses the
  ``provider:module:function:name`` form and `_verify_dtrace_probes`
  greps `SDT_PROBE_DEFINE\d?\b` macros under `sys/`. Catches fabricated
  probes like `sysinit:::entry`/`sysinit:::return`.

Both feed into `total_issues` and surface in `_build_fact_check_prompt`
as `kernel_options_not_found` / `dtrace_probes_not_found` blocks.
`run_chapter`'s log lines mirror the categories.

### 16. Agent step caps

- Writer (`create_writer_agent`) — `max_steps=40`. Used for draft,
  every revision, and fact-fix. Long subsystem chapters routinely use
  35+ steps because the writer explores `sys/<subdir>/` before writing.
- Reviewer (`create_reviewer_agent`) — `max_steps=15`. Was 10 but
  hit the cap on review 2 with a long rubric.

If you see `hit max_steps` consistently on the writer, first check
whether the fact-check pass is feeding it 30+ "missing" symbols — that
usually means a regression in the noise filter, not that the cap is
too low.

## Atomic writes

All file writes in the pipeline use `_atomic_write()` (tempfile + fsync +
`os.replace`). When adding new outputs, route them through this helper rather
than `open(...).write(...)` — Ctrl-C mid-write would otherwise leave a
truncated file that crashes the next run before it can self-repair.

## Checking changes end-to-end

For non-trivial changes to `generate-doc.py`, run a single chapter on framework
before declaring done:

```
scp generate-doc.py framework:DaemonDocs/
ssh framework "cd DaemonDocs && python3 generate-doc.py --chapter 1 --force"
```

Watch for: `hit max_steps` warnings, `UNVERIFIED DRAFT` annotations,
`Traceback` / `AttributeError`. The pipeline is verbose by design — silent
success means it worked.

## Files to know

- `generate-doc.py` — the whole pipeline (~3600 lines).
- `chapters.yaml` — chapter definitions; `output_file` is relative to
  FreeBSD src root.
- `IMPROVEMENTS.md` — audit checklist; mostly closed out, but worth re-reading
  before proposing a "new" idea.
- `.index/` — TF-IDF artefacts (corpus, vocab, matrix). Safe to delete; rebuilds.
