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
   `RESOLVED_PROVENANCE`, `SYSCTL_GRAPH` + `_sysctl_graph_available`
   (optional codebase-memory-mcp backend for sysctl fact-check). Env
   vars: `FREEBSD_SRC`, `BOOKS_DIR`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`,
   `OPENAI_MODEL`, `CODEBASE_MEMORY_MCP_BIN`, `SYSCTL_GRAPH_PROJECT`,
   `SYSCTL_GRAPH_TIMEOUT`.
2. **Book corpus extraction** — PDF/CHM/EPUB → text; FreeBSD
   handbook + man pages + git logs. Builds the searchable corpus.
   **Invariant: an unchanged corpus must be byte-identical run to
   run.** Two functions write it (`build_book_corpus`, then
   `extract_freebsd_docs` which strips and re-appends the FreeBSD
   entries); both compare against what is on disk and skip the
   write when nothing changed. See "Corpus/index invariants" below
   — violating this is silent and expensive.
3. **TF-IDF index** — `class TfidfIndex`, `get_or_build_index`.
   Persistent on-disk index used by the `search_books` tool.
   Staleness is decided by **mtime** against the corpus, which is
   why the invariant above matters.
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
   `_build_fact_check_prompt`. All verifiers are grep-over-`sys/`
   EXCEPT the sysctl OID pair (`_extract_sysctls` /
   `_verify_sysctls_via_graph`), which queries the optional
   codebase-memory-mcp graph — the one symbol class grep cannot check.

(Two more banner sections follow that aren't part of the
per-chapter pipeline: **Cross-README navigation** —
`build_navigation`, `_add_see_also_links`,
`_build_chapter_rels`, `_sanitize_chapter_links` (broken `.md`
link repair), `_sanitize_doc_urls` (drop docs.freebsd.org links
whose handbook/articles slug isn't in `$FREEBSD_DOC`),
`_link_see_also_source_paths` (wrap bare backtick source paths
in See Also as relative markdown links),
`_link_manpage_refs` + `_build_manpage_index` (wrap inline
`name(N)` man-page references in chapter prose as relative links
to the source-tree mdoc file) — and **Chapter index** —
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
                       + jargon/acronym gloss scan (no source tree needed;
                         reported to the REVIEWER, not the fact-fix loop)
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
| Add a reviewer rubric criterion | `build_review_prompt` | Add to the numbered rubric AND to the JSON output schema (`"criteria": { ... }`) AND to the gate. The gate is `_review_passes`, which iterates `criteria.values()` — adding a key there is automatic. The fail-counter `_criteria_fail_count` hard-codes the current count (`8`) for the "broken JSON" worst-case; bump it if you add criteria. Also bump the `N - fail_count`/`/N` print formatting in `run_chapter`'s review loop and rollback log, and the `best_fails = N+1` initializer (one more than the max possible). If the criterion is conditional (only fires when a certain section is present), gate it on `"Section Name" in sections` and emit a `PASS: not required` JSON line in the off branch — the `wants_diagram` (Mermaid) gate is the canonical example. |
| Inject ground truth into the reviewer | `build_review_prompt` | Pre-validate before the prompt is rendered, build a `verified_FOO_block` string, interpolate it alongside the existing `verified_block` / `symbol_block` / `macro_block`. The Accuracy criterion and the **No hedges** block reference these blocks by name — update both when adding a new ground-truth category. |
| Inject ground truth into the writer | `_build_symbol_catalog` (called by `build_chapter_prompt`) | Pre-extract real symbol names from the chapter's `source_files` + bounded `source_dirs` sample, render an `## Authoritative Symbol Catalog` block. Caps live in `_CATALOG_MAX_*` and `_CATALOG_FILES_PER_DIR` — keep them tight; this is prompt-cost, not fact-check-cost. To add a new symbol kind, extend `_dirmap_extract_names` (which is shared with `directory_map`). |
| Add a new section type (output template) | `_SECTION_CATALOG` | Each entry has `template_body`, `rubric_body`. Per-chapter section list comes from `_chapter_sections(chapter)` reading `sections:` in `chapters.yaml`. |
| Add a new fact-check verifier | Banner **Fact-checking** | Pair: `_extract_FOO(text) -> List` and `_verify_FOO(items, src_root, extra_search_dirs=None) -> List[missing]`. Use `_verify_with_cache` if the verifier is a grep over `sys/` (free re-runs via `_FACT_CHECK_CACHE`). Wire into `fact_check_draft` and `_build_fact_check_prompt`. Existing examples: structs, functions, kernel options, DTrace probes, MALLOC_DEFINE tags, sysctl OIDs (graph-backed — see next row). |
| Add a **graph-backed** verifier (something grep can't check) | Banner **Fact-checking**, model on the sysctl OID pair | `_extract_sysctls` + `_verify_sysctls_via_graph` query the `codebase-memory-mcp` graph via its `cli <tool>` subcommand instead of grep — sysctl OIDs (`vm.pmap.pde.mappings`) are assembled from `SYSCTL_*` macros and exist nowhere as a literal, so grep provably can't verify them; the indexer reconstructs the OID tree into `Sysctl` nodes. The backend is OPTIONAL: gate every graph call on `_sysctl_graph_available()` (cached probe that prints ONE warning when off) and return `[]` when unavailable so the pipeline never blocks. Cache per-item in a dedicated dict (`_SYSCTL_GRAPH_CACHE`), NOT the grep `_FACT_CHECK_CACHE` (different backend + key space). "Not found" is reported as *suspect* not *hallucinated* because ~1/3 of OID nodes are `resolved:false`. Config + probe live in the **Configuration** banner (`SYSCTL_GRAPH`, `_sysctl_graph_available`). |
| Verify symbols outside `sys/` (e.g. `stand/`) | `chapters.yaml` `extra_search_dirs:` | Per-chapter list of additional grep roots, joined to `~/freebsd-src`. `_resolve_search_roots` always includes `<src>/sys` and appends each existing extra. The cache key embeds a sorted-tuple suffix so widening roots for one chapter does NOT poison the sys-only cache for others. Use this when a chapter's subject (boot loader, userland tool) lives outside `sys/`. |
| Diagnose a chapter that stops with no output | Nothing — it is automatic | `install_hang_detector()` runs from `main()`. When the main thread stops calling `beat()` for `DAEMONDOCS_HANG_DUMP_SEC` (default 1800s, `0` disables), a daemon thread prints the main thread's stack to the chapter log and keeps going. It never kills anything. Wrap a new long phase in `with heartbeat("label"):` so the dump names it, and call `beat()` inside long loops. Works even when stuck in a C-level regex, which a Python-level timeout cannot interrupt. **Before dumping it asks the endpoint whether the model is actually decoding** — see the note below. Startup prints which mode is active; if it says "WITHOUT endpoint liveness", restart llama-server with `--metrics`. |
| Verify a claimed **path** (file or directory) | `_extract_file_paths` + `_verify_file_paths` | Extraction has three branches: backticked-with-extension, bare-with-extension, and (added 2026-08-22) directories + extensionless files from `_EXTENSIONLESS_FILES`. A single-segment claim only counts as a directory if the writer wrote the trailing slash (`` `librescue/` ``); bare words are skipped or every backticked command becomes a "path". Verification carries four exemptions that keep it quiet, and **all four are load-bearing** — see the "Things that look weird" note. Do NOT gate extraction on `_FREEBSD_TOP_DIRS`: that tuple still lists `gnu/`, a subtree FreeBSD retired, so filtering through it whitelists exactly the hallucination you want to catch. |
| Catch struct bodies hallucinated wholesale | `_verify_struct_bodies` returns `(bogus, abridged)` | The bogus-field check (per-field grep) misses bodies where every claimed field is fabricated together. The overlap-threshold layer flags any non-abridged body whose claimed-field set has zero overlap with the real top-level fields (≥4 real fields). `_struct_body_is_abridged` recognizes `...`, `\u2026`, and comments containing `elided`/`omitted`/`for brevity`/etc — writers can opt out by elision. Wired into `fact_check_draft` as `struct_bodies_abridged`. |
| Catch jargon used without a definition | `_extract_unglossed_jargon` + `_extract_unexpanded_acronyms`, wired as `jargon_unglossed` / `acronyms_unexpanded` | The only check here that asks "will the reader understand this?" rather than "is this symbol real?". Two scans: a curated `_JARGON_TERMS` set (whole-word, case-sensitive for short all-caps like `KVM`) and repeated-but-never-expanded `[A-Z]{2,}` acronyms. Both mask fenced blocks via `_mask_fenced_blocks` (which preserves offsets — the gloss window is measured against them). A term counts as glossed by `_has_gloss`: verbal cues anywhere in `_GLOSS_WINDOW_CHARS`, punctuation cues (`(`, `:`, em-dash) **only immediately after the term** — the first version accepted them anywhere in the window and consequently missed the ch12 `KVM` case it was written for. Findings are deliberately OUT of `total_issues` (that gates the fact-fix loop, which is about deleting false claims) and go to the reviewer as an "Undefined Terms Detected" block built in `build_review_prompt`. When adding terms, measure the false-positive rate across all shipped chapters first — calibration went 10.4 → 7.0 findings/chapter by *removing* over-broad entries (`sysctl`, bare `vfs`/`zone`, SCSI command words). Tests: `tests/test_jargon_gloss.py`. |
| Catch arity-mismatched function signatures ("verified hallucination") | `_extract_function_signatures` + `_real_function_signature` + `_verify_function_signatures`, wired as `func_sigs_mismatch` | Names of real FreeBSD functions can be combined with stale 2022-era arg lists from the model's training data — every existing check passes (name, paths, fields) while the chapter teaches the wrong API. The new pass parses fenced ```c definitions only (call-site arity is too noisy), counts top-level commas with `_count_c_args` (skipping `void`, K&R-style, and nested parens for function-pointer args), and compares against the real definition arity grepped from the source tree. Cache: `_FUNC_SIG_CACHE` keyed `(cache_root, func_name)`. Returns `None` whenever lookup or parse is uncertain so we never false-flag. Skips names already in `funcs_not_found` to avoid double-reporting. |
| Change sampler params | `create_writer_agent` / `create_reviewer_agent` | Pass kwargs to `OpenAIServerModel(...)`. They land on every API call via `self.kwargs` in smolagents' `Model._prepare_completion_kwargs`. **Only for parameters the openai SDK knows.** Anything provider-specific (`chat_template_kwargs`, llama.cpp extensions) must go inside `extra_body={...}` — the SDK validates kwargs against `Completions.create()`'s signature and raises `TypeError` on unknown names, so a top-level pass fails every call *before it reaches the wire*. It still shows up in `model.kwargs`, so inspecting that proves nothing; test end-to-end against a live endpoint. |
| Bound how much one writer step can generate | `WRITER_MAX_TOKENS` (Configuration) | Env `DAEMONDOCS_WRITER_MAX_TOKENS`, default 16384, `0` = unlimited. Nothing else caps this — smolagents has no default and llama-server's `n_predict` is unset — so before 2026-08-23 a single step could generate to the full 131k context. Sized from measurement: normal steps are 200-1200 tokens, a whole chapter draft ~6.7k (largest measured). Pass it as a real kwarg (the SDK knows `max_tokens`), unlike `chat_template_kwargs` which needs `extra_body`. The reviewer is deliberately uncapped — `max_steps=5` already bounds it. |
| Turn model reasoning on/off per agent | `WRITER_THINKING` / `REVIEWER_THINKING` (Configuration banner) | Env-gated (`DAEMONDOCS_WRITER_THINKING`, `DAEMONDOCS_REVIEWER_THINKING`), both default on, applied via `extra_body={"chat_template_kwargs": {"enable_thinking": …}}`. Writer reasoning must stay **on** — see the post-mortem in FUTURE_IMPROVEMENTS.md. The two gates exist so the endpoints can run opposite reviewer arms against a shared queue for a real A/B. |
| Widen criterion 8 to flags/modes and enumerate its findings | `RATIONALE_ENUM` (Configuration banner) | Env `DAEMONDOCS_RATIONALE_ENUM`, default **off** — the only flag here that defaults off, and the only one that changes *prompt text* (every other prompt conditional is driven by chapter data or computed draft facts). Reads `== "1"`, not `!= "0"`. When on, `build_review_prompt` widens criterion 8 to cover paired flags (`BIO_UNMAPPED`, `M_NOWAIT`/`M_WAITOK`, `LK_EXCLUSIVE`/`LK_SHARED`) and adds a **top-level** `rationale_missing` list to the output schema. It must stay top-level: `_review_passes` rejects any non-string value inside `criteria`, so nesting it there would fail the gate for every chapter. Off by default because it is an unmeasured prompt edit awaiting an A/B — `scripts/regen-runner.sh` takes it as positional arg 4. |
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
- **`_strip_comparison_section` is a legacy-content safety net** —
  the mandatory `## Comparison` section was removed from the
  pipeline in 2026-05 (cross-OS claims were the dominant source
  of unverifiable hallucination). New chapters don't produce the
  section, so the stripper is a no-op for fresh drafts. It still
  runs before symbol extraction so on-disk drafts written before
  the section was removed don't false-positive on Linux/macOS
  symbol names during re-fact-check passes.
- **`_extract_function_names` requires `()` evidence** — bare
  backticked identifiers are skipped because they're dominated
  by struct fields, type names, sysctls, and parameter names.
  Grepping all of those wastes fact-fix steps. It also unions in
  `_extract_fenced_function_defs` results so a fabricated function
  *body* in a ` ```c ` block (`static int bi_construct(void) { ... }`)
  is flagged the same way as a backticked call.
- **`name(N)` in backticks is a man-page citation, not a call** —
  `_MANPAGE_CITATION_RE` drops it. A citation is shape-identical to a
  zero- or one-argument call, so ch40 (2026-08-30) sent the reviewer
  `newbus()`, `usb()` and `usbdi()` harvested from its own See Also
  list. The test is run against the *paren contents*, not the whole
  match, and requires a bare section (`1`-`9` plus optional suffix,
  `sysctl(3lua)`): that keeps `free(9)` and `free(ptr)` on opposite
  sides of the line. `_link_manpage_refs` already treats the same
  notation as first-class, so the two agree on what it means.
- **Three layers of struct-field verification, distinct shapes:**
  (1) `_extract_struct_bodies` + `_verify_struct_bodies` — claims
  inside a literal `struct NAME { ... }` block; (2) `_extract_struct_field_claims`
  + `_verify_struct_field_claims` — member-access expressions
  (`var->field`, `var.field`) where `var` is bound to a struct via
  an in-block `struct NAME *var` declaration, plus prose forms like
  `STRUCTNAME->FIELD` (the writer using the struct name as if it
  were a variable, e.g. `bootinfo->bi_efi_memmap`); (3) the
  `_FILE_EXT_DENYLIST` (`c`, `h`, `S`, `py`, …) excludes the
  `path/file.c`-shaped collisions where a backticked file path
  is misread as `var.field`. ch2 (Boot Process, 2026-05-02) shipped
  with all three escape hatches simultaneously open and motivated
  the layered design.
- **`_extract_struct_names` matches both `struct NAME` and
  ``\`NAME\` structure``** — backticked-prose form catches the
  writer naming a fictional type without the `struct` keyword
  ("a `bi_module` structure"). Bare-prose "data structure" / "tree
  structure" is rejected because it lacks the backticks; the
  writer's backticks are the load-bearing signal.
- **`_verify_structs`' shape filter must match all three definition
  spellings, and it is a BSD `grep -E` pattern.** Stage 2 of
  `_batched_grep_present` narrows stage 1's hits to definition-looking
  lines; anything it drops is invisible to stage 3 and comes back
  "missing". FreeBSD writes struct definitions three ways, and the
  original `^struct ` pattern only matched the first: plain
  (`struct foo {`), tab-separated (`struct\tfoo {`, 42 tags),
  indented-because-nested (`\tstruct foo {`, 442 tags), and
  `typedef struct foo {` (3735 tags -- the biggest gap). ch37
  (2026-08-30) was failed on Accuracy for citing `struct in_endpoints`,
  which is real. **The tab must be a literal tab character**, not
  `\t`: a BSD `grep -E` bracket expression does not interpret the
  escape, so `[ \t]` matches space, backslash or `t` and never a tab
  -- and a test exercising the pattern through Python's `re` passes
  while the pipeline stays broken. `tests/test_struct_shape_grep.py`
  therefore asserts on the pattern the function actually passes to
  grep. Leading whitespace and the `typedef` prefix are allowed **only
  on the brace alternative**; the K&R alternative (`struct foo` at
  end of line) keeps `^` because an indented `struct thread *td`
  parameter would otherwise verify as a definition.
- **`struct` is also an English noun, and `_ENGLISH_AFTER_STRUCT`
  is how the extractor tells the two apart.** "The struct above",
  "the on-the-wire struct defines", "a four-struct tree" are prose;
  the word after `struct` is not a type name. ch40 (2026-08-30)
  shipped UNVERIFIED partly because all three reached the reviewer's
  "Missing structs" list, and the Accuracy criterion is told to FAIL
  on that — the reviewer graded correctly against fabricated input.
  Two details are load-bearing. The guard is `(?<![\w-])`, not `\b`:
  `\b` matches inside a hyphenated compound (`four-struct` → boundary
  before `struct`) and after a word char (`sub_struct`), and the
  hyphen case is the one that actually shipped. And **every word on
  the list must be verified never to be a real tag** before it is
  added — FreeBSD names core types with plain words (`buf`, `file`,
  `proc`, `thread`, `mount`, `link`, `name`), so a careless entry
  blinds the checker to a real type rather than to prose. Run
  `grep -rwE 'struct (word1|word2) \{' ~/freebsd-src/sys`; group 4
  of `tests/test_extractor_english.py` re-runs that over the whole
  list on every test run.
- **The hang detector asks the LLM endpoint before crying wolf, and it
  reads `n_decode_total` — not `tokens_predicted_total`.** A wall-clock
  threshold cannot separate "model thinking hard" from "process wedged":
  llama-server decodes at ~7 tok/s here, so a long reasoning block is
  many minutes of legitimate silence. `_endpoint_is_decoding()` samples
  llama-server's `/metrics` twice, 8s apart, and suppresses the dump only
  if the counter advanced. `tokens_predicted_total` looks like the
  obvious choice and is wrong — it only rolls up when a request
  *completes*, so it stays frozen for the entire generation you are
  trying to observe (measured: frozen at 191310 while `n_decode_total`
  climbed 135 per 20s). The probe is deliberately conservative: no
  `/metrics`, unreachable host, or a flat counter all read as "not
  decoding" so the detector still fires. That direction matters because
  the bug it exists for — a local CPU spin with the endpoint idle —
  presents as exactly a flat decode counter.
- **`_verify_file_paths` exempts four path shapes, and each one
  was earned.** Absolute paths (`/boot/kernel`, `/usr/bin`) name
  installed-system locations and are legitimately absent from a
  source checkout; `src/`-prefixed paths are relative to
  `/usr/src`; kernel-relative paths are how chapters write C
  include paths (`sys/proc.h` is really `sys/sys/proc.h`,
  `vm/uma.h` is `sys/vm/uma.h`) and bare subdir names (`kern`,
  `amd64`); `machine/foo.h` is the per-arch alias resolved at
  build time to `sys/<arch>/include/`. Without them the verifier
  flags ~30 correct claims per chapter and burns the writer's
  fact-fix budget rewriting accurate prose. One consequence is
  accepted: a retired top-level directory whose name still exists
  under `sys/` (bare `gnu` vs `sys/gnu`) escapes. Using the
  writer's trailing slash to separate the two was tried and
  reverted — chapters write `kern/`, `vm/`, `amd64/` the same
  way, so it cost ~20 false positives to gain one true one.
- **Glob "corrections" must share the claimed parent directory.**
  `_verify_file_paths` emits `wrong → right` suggestions from a
  glob fallback. Matching on basename alone across the whole tree
  produced `sys/conf/config → sys/contrib/openzfs/config`, which
  sends the writer to rewrite prose into an unrelated file. A
  wrong correction is worse than no correction: the writer trusts
  it.
- **Fact-check messages carry the ANSWER, not just the verdict.**
  `_verify_struct_bodies` already parses the real field list in
  order to detect a mismatch, so the flag string includes it:
  `struct sysinit: si_sub, si_order (real fields are: func, next,
  order, subsystem, udata)`. Before this, "these fields do not
  exist, read the defining header" was unactionable —
  `read_freebsd_source` truncates large headers like
  `sys/sys/kernel.h` before reaching the definition, and on
  2026-08-22 ch3 the writer looped ~24 steps insisting the stale
  names were right ("the fact-checking says they don't exist.
  This is very confusing."). Any new verifier that computes ground
  truth to detect an error should hand that ground truth over.
  Note `tests/test_struct_factcheck.py` matches on the text *before*
  `(real fields are:` — otherwise every real field name trivially
  "appears in the issues".
- **`_FACT_CHECK_CACHE` is process-global, keyed by
  `(kind, src_root, symbol)`** — same symbol verified once per
  run regardless of which chapter or revision round asked for it.
  This is why pre-validating symbols in `build_review_prompt` is
  effectively free for the post-review fact-check.
- **The sysctl verifier is the only one that shells out to a graph,
  and the only one that can silently verify nothing** — sysctl OID
  paths (`vm.pmap.pde.mappings`) are built at compile time from
  `SYSCTL_NODE`/`SYSCTL_INT`/... macro chains and appear nowhere in
  the source as a literal string, so grep returns 0 for a REAL OID
  exactly as for a fabricated one. codebase-memory-mcp reconstructs
  the OID tree into `Sysctl` graph nodes; `_verify_sysctls_via_graph`
  queries them via the binary's `cli search_graph` subcommand. When
  the binary or index is absent, `_sysctl_graph_available()` returns
  False (after one warning) and the verifier returns `[]` — by design,
  so a machine without the graph still runs the full pipeline, just
  without sysctl checking. A "not found" is phrased to the writer as
  *suspect, verify against `sysctl(8)`* rather than *hallucinated*,
  because ~1/3 of `Sysctl` nodes are `resolved:false` (parent chain
  built from a macro arg the indexer couldn't resolve) and a real OID
  can land among them — a false-negative, which is the safe direction
  for a hallucination gate.
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

## Corpus/index invariants — two bugs live here

Both were found on 2026-08-22, both were silent, and both are
easy to reintroduce with an "obviously harmless" edit.

**1. The `### SOURCE: X ###` delimiter must survive a strip
cycle.** `extract_freebsd_docs` removes stale FreeBSD entries with
`re.split(r"### SOURCE: (.+?) ###", ...)`. `re.split` *consumes*
the delimiter and returns only the capture group, so reassembling
from the pieces without re-emitting `### SOURCE: … ###` silently
destroys the header of every retained segment. It did: all 9 books
collapsed into one unlabeled blob (0 book headers in an 18 MB
corpus). Consequences are indirect and therefore easy to miss —
`build_book_corpus`'s `drop_sources` can no longer prune a book,
and `search_books` can no longer attribute a hit. A clean
`--reindex` after the fix took the index from 2031 to 6521 chunks
(12 656 → 28 597 terms): the writer had been searching roughly a
third of the intended corpus.

**2. Never write the corpus unconditionally.** `get_or_build_index`
treats "corpus mtime > index mtime" as stale, and `_atomic_write`
renames into place, which always freshens mtime. An unconditional
write therefore rebuilds the ~100 MB TF-IDF matrix on *every run*,
and with two concurrent runners (one per endpoint) both rebuild
and race on the same `.npy`. The same strip/re-append also grew
the corpus 2 bytes per run forever by stacking separators — found
at 100 consecutive newlines, i.e. ~50 accumulated rebuilds. Fix
was to normalize separators and compare content before writing;
if you add a third corpus writer, it needs the same guard.

---

## Execution topology — where the script runs

`generate-doc.py` runs on this host. The repo,
`$FREEBSD_SRC` (`~/freebsd-src`), `$BOOKS_DIR` (`~/books`), and
`~/freebsd-doc` all live here. The LLM endpoints are separate hosts
on the LAN, reached purely via `OPENAI_BASE_URL` — they do NOT have
the repo, the source tree, or a copy of the script. Do not SSH into
them to launch jobs. Current endpoints:

- `framework`  → `$FW_URL`
- `framework2` → `$FW2_URL`

The actual LAN addresses are deliberately not in this public repo —
see the note in `CLAUDE.md` for where they live.

Parallelism is N `generate-doc.py` processes on this host, each
pointed at a different `OPENAI_BASE_URL`. The script itself only
cares about the env var.

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
