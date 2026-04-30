# DaemonDocs — Future Improvements

Ideas for expanding coverage and raising the value of generated chapters.
Originally captured during a discussion about a 13-chapter set; the set
has since grown to **25 chapters** (Tier-1 Phase A is largely complete).
The remaining backlog is in pattern chapters, navigation, and pipeline
quality.

This file also tracks **pipeline quality issues** observed in real runs
that haven't been fixed yet, or that need data from more runs before a
fix is justified. Items marked **[DONE]** are kept as post-mortems and
rationale records — do not delete them; they explain why the current
pipeline is the way it is, and several have load-bearing comments in
`generate-doc.py`.

---

## Pipeline quality issues observed in real runs

### [PARTIALLY MITIGATED] Writer hallucinates heavily on the initial draft (chapter-dependent)

Observed on the buffer cache chapter (`sys/vm/README_bcache.md`,
generated 2026-04-28). The round-1 draft fabricated:

- dozens of `struct buf` fields that don't exist in `sys/sys/buf.h`
  (e.g., `b_dirtyblkhd`, `b_vmelem`, `b_actf`, `b_actb`, `b_freelist`,
  `b_hash`, `b_queue`, `b_vnbufs`, plus invented linked-list variants
  like `b_bcount_next`, `b_bufobj_next`, etc.)
- function names that don't exist (`bufq_insert_dirty()`,
  `bufbdflush()`)
- a fictional `buf_ops_bio` structure with `.bop_write`, `.bop_strategy`,
  etc. — the real buffer cache has no such pattern
- fake DTrace probes (`buffercache:::buf-start`, `buf-done`, `buf-dirty`,
  `buf-clean`)
- wrong allocator (`MALLOC_DEFINE(M_BIOBUF, ...)` instead of UMA)

The reviewer caught all of these, but 3 revision rounds couldn't fully
repair such a thoroughly hallucinated draft. The chapter ended up
UNVERIFIED with revisions regressing 3/7 → 5/7 → 4/7.

**Why this matters:** the existing pipeline assumes the initial draft
is *broadly correct* and revisions are *patches*. When the writer
fabricates structurally — making up entire data structures and function
families — revision-as-patching can't keep up. Each round either
introduces new fabrications, or removes some real content along with
the invented content.

**Possible fixes** (don't pick one until more runs confirm the pattern
isn't chapter-specific):

1. **Require the writer to quote actual source.** Tighten the writer
   prompt: any code block claiming to be from a specific file must be
   verified by reading that file via the writer's tools. No
   paraphrasing struct definitions from training-data memory.
2. **Run fact-check before review, not after.** The current order is
   draft → review-loop → fact-check. If fact-check ran first (or in
   parallel with the first review), the round-1 hallucinations would
   surface before they had a chance to ossify across revisions. Cost:
   another agent pass per chapter.
3. **Per-chapter writer-prompt tuning.** Chapters with lots of small
   struct fields or naming-convention-heavy content (buf, mbuf, vnode)
   may need a stricter "quote-don't-paraphrase" prompt than chapters
   that are mostly architecture prose.

**Status:** mitigated, not eliminated.

What shipped between the post-mortem above and now:
- **Best-draft tracking** prevents a regressing revision from being
  written (see [DONE] entry below).
- **Symbol pre-validation** in the reviewer prompt (structs, functions)
  catches struct-field and function-name fabrications on review pass 1
  — the writer no longer accumulates rounds of unchallenged hallucination.
- **Quote-don't-paraphrase rule** in the writer prompt (Option 1 from
  the original list above) instructs the writer to verify any source
  excerpt against `read_freebsd_source` before quoting.
- **`directory_map(path)`** tool gives the writer a structured view of
  a directory before it commits to specific file names or struct/function
  identifiers, reducing the rate at which it invents plausible-sounding
  symbols.
- **Authoritative Symbol Catalog injected into the writer prompt**
  (`_build_symbol_catalog` → `## Authoritative Symbol Catalog` block in
  `build_chapter_prompt`). Before the writer starts, we walk the chapter's
  `source_files` plus a bounded sample of `source_dirs`, extract real
  struct and function names via the same `_dirmap_extract_names` used by
  `directory_map`, and render them into the prompt. The writer now sees
  ground-truth symbol names *before* drafting — closing the gap that
  `directory_map` left open (the writer had to choose to call it). Caps
  are in `_CATALOG_MAX_*` and `_CATALOG_FILES_PER_DIR` to keep prompt
  cost bounded.

What's still observed in the 2026-04-29 validation run: chapters 1, 5,
and 6 (READMEs in `/`, `sys/kern/`, `sys/vm/`) still occasionally
exhaust the review loop and fall through to best-draft fallback. The
fact-fix step occasionally hits `max_steps=40` (output-may-be-truncated
warning surfaced). The chapter content is now broadly correct — the
remaining failures are about *thoroughness*, not *fabrication*.

**Open ideas not yet tried:**
- Run fact-check before review (the original Option 2). Would catch
  remaining symbol drift earlier. Cost: an extra agent pass per chapter,
  plus the fact-check cache is already process-global so re-runs within
  the same chapter are free.
- Per-chapter writer-prompt tuning for "small-fields" chapters (buf,
  mbuf, vnode). Hasn't been needed yet — symbol pre-validation may have
  closed the gap.

### [DONE] Revisions can regress (best-draft tracking)

Observed on chapter 5 (process management) and chapter 6 (buffer cache)
in the same 2026-04-28 run. Reviewer rounds went:

| Chapter | Round 1 | Round 2 | Round 3 |
|---|---|---|---|
| Process Management | 3/7 | 6/7 | **5/7** |
| Buffer Cache | 3/7 | 5/7 | **4/7** |

The pipeline wrote round 3 in both cases, even though round 2 was
strictly better. Without best-draft tracking, the writer's revisions
can leave you with a *worse* draft than an earlier round.

**Status:** fixed. `run_chapter` keeps the lowest-fail-count draft seen
so far and writes *that* on exit, even if the final revision regressed.
Behavior is documented in `CODE_MAP.md` ("Best-draft tracking"). See the
load-bearing comment near the rollback in `run_chapter`.

### [DONE] Reviewer hallucinates "file does not exist" claims (path validation)

Observed on chapter 5 round 2 (2026-04-28). The reviewer asserted
*"`sys/kern/kern_thread.c` does not appear to exist in the FreeBSD
source tree"* — but that file is real (1777 lines). The reviewer
agent has no source-tree access (only `search_books`), so it cannot
verify file presence. When it confidently claims non-existence, the
writer "fixes" the non-issue by inventing alternate filenames,
introducing fresh hallucinations.

**Status:** fixed. `build_review_prompt` pre-validates every
`source_files` and `source_dirs` path and injects a `verified_paths_block`
into the rendered prompt. The Accuracy criterion is reminded that it
cannot verify non-existence outside the injected missing-list. The same
ground-truth-injection pattern was later extended to struct/function
symbols (see "[DONE] reviewer has no source-tree access" below) — both
follow the same shape, so add new ground-truth categories the same way.

### [DONE] The reviewer can hedge with "may use X" instead of FAIL

Observed in chapter 6 round 3: *"Verify the function name
`bufcache_init` — FreeBSD may use `bufinit()` or a different
initialization function; confirm against actual sys/kern/vfs_bio.c."*

The reviewer can't actually confirm against the source — it has no
source tool. So it hedges. The writer takes the hedge as instruction
to change something potentially correct, gambling on whichever name
the reviewer guessed.

**Status:** fixed. `build_review_prompt` now contains an explicit
**No hedges** block prohibiting "may use", "verify against", "confirm
this", and similar non-assertions in `issues`. The block also references
the injected `verified_paths_block` and `verified_symbols_block` so the
reviewer knows where it *does* have ground truth. The forbidden-hedge
wording must stay in sync with the ground-truth injection — see
CODE_MAP.md ("Inject ground truth into the reviewer").

### [DONE] The reviewer has no source-tree access at all

The deeper cause behind the three issues above (false "file does not
exist", "may use X" hedges, hallucinated struct field corrections) is
that the reviewer is invoked as a plain LLM completion — no tools, no
agent loop. It receives only the rendered prompt (writer's draft +
chapter metadata + the path-validation block we just added). It
literally cannot grep, cannot read a file, cannot confirm a function
or struct-field name exists.

**Why it was designed this way:**

- **Speed/cost** — a tool-using reviewer doubles the agent-loop
  overhead per chapter. With 13 chapters × up to 4 revision rounds,
  that adds up.
- **Separation of concerns** — the reviewer was meant to judge the
  *draft* against the *prompt*, not redo the writer's research. If
  both grep, you pay twice for the same exploration.
- **Determinism** — a tool-less reviewer is a pure function of draft +
  prompt, easier to prompt-tune.

**Why it's now the bottleneck:** every "reviewer hallucinates / hedges"
issue above (items 3 and 4, plus the writer-hallucination repair loop
in item 1) traces back to this. Hedges are the reviewer being honest
about what it can't verify; without verification, hedge is the most it
can offer.

**Possible fixes** (increasing cost):

1. **Pre-validate more than paths.** Extend the path-validation we
   just added: in `build_review_prompt`, extract every struct name,
   function name, SDT probe, and `MALLOC_DEFINE` tag the writer
   mentions, grep-verify them against the chapter's `source_dirs`,
   and inject `verified_symbols` / `missing_symbols` blocks the same
   way we now do for paths. The reviewer gets ground truth without
   needing tools. Fits the existing architecture; matches what
   already worked for paths.
2. **A small, scoped grep-only reviewer agent.** Promote the reviewer
   to a `CodeAgent` but restrict it to `grep` over the chapter's
   `source_dirs` only — no `read_file`, no `search_books`, no
   exploration. One pass, then verdict. Catches things item 1's
   regex extraction misses (e.g., relationships between symbols).
3. **Full tool-using reviewer agent.** Same toolset as the writer.
   Slow, redundant, probably not worth it.

**Status:** Option 1 (pre-validate symbols) is fully implemented.
`build_review_prompt` now injects three parallel ground-truth blocks
into the reviewer prompt:

- `verified_block` — paths from `source_files` / `source_dirs`
- `symbol_block` — struct names and function names extracted from the
  draft, grep-verified against `source_dirs`
- `macro_block` — kernel options (`option FOO`), SDT probes
  (`provider:::name`), and `MALLOC_DEFINE` tags (`M_FOO`), all extracted
  from the draft and grep-verified the same way

All three categories share the process-global `_FACT_CHECK_CACHE`
(keyed by `(kind, src_root, symbol)`), so the post-review fact-check
pays nothing extra to re-verify what the reviewer already saw.

The Accuracy criterion text and the **No hedges** block in
`build_review_prompt` reference all three blocks by name — when adding
a new ground-truth category, update both. New `MALLOC_DEFINE` extraction
includes a `_MALLOC_TAG_IGNORE` set for allocator flags (M_NOWAIT,
M_WAITOK, M_ZERO, …) which look like tags but aren't.

Validation runs (chapters 1, 5, 6, 7 on 2026-04-29) confirmed the
reviewer catches symbol-level fabrications cleanly (e.g. `struct
dinode`, `struct znode`, `NDRESET()`, `vfs_domount()`). Macro coverage
shipped 2026-04-30; the regen run currently in flight is the first
data point on its impact.

Option 2 (grep-only reviewer agent) and Option 3 (full tool-using
reviewer) are still on hold — Option 1 has been good enough so far.

### [DONE] Writer can drift into "I'm a coding agent that writes files" mode and emit a status string instead of the chapter

Observed on chapter 7 (VFS) when running against a second llama-server
endpoint (192.168.100.136) on 2026-04-28. Same model
(`Qwen3.6-35B-A3B-UD-Q4_K_XL`) as the primary endpoint, but slightly
different sampling defaults exposed by `/props`.

What happened, step by step:

1. The writer agent assembled the chapter content into a Python string
   variable.
2. It tried `with open('/tmp/network_stack_chapter.md', 'w') as f:
   f.write(content)` — `open()` is not in
   `additional_authorized_imports` so smolagents raised
   `InterpreterError: Forbidden function evaluation: 'open' is not
   among the explicitly allowed tools or defined/imported in the
   preceding code`.
3. Instead of recovering by calling `final_answer(content)`, the model
   tried more disk-writing variants for ~10 more steps, eventually
   printed `"File successfully written and verified!"` (purely
   imagined — there is no file-write tool), and then called
   `final_answer("README.md successfully written to ... with all
   required sections: ...")` — a *status string*, not the chapter.
4. The pipeline's `_looks_like_stub` detector caught the truncated
   draft and aborted the chapter. **0/1 chapters generated.** No file
   was written.

Why it happened: probably temperature/top-p drift between the two
llama-server instances pushing the model into the wrong "shape" of
agent behavior. The same model on the primary endpoint does not
exhibit this — it returns the chapter via `final_answer()` correctly.

**Status:** all three fixes shipped:

1. **Writer prompt explicit about `final_answer()`** — the writer prompt
   now contains a "How to return your work" block that names
   `final_answer()` and forbids file I/O. The "Code blocks must be
   Python — NOT shell" rule was added on the same pass.
2. **Stub-retry on initial draft** — `_looks_like_stub` flags drafts
   that look like status strings (`"README.md successfully written"`)
   instead of chapter content. Initial-draft stubs get one retry;
   revision / fact-fix stubs abort that step and roll back to the best
   draft. Documented in `CODE_MAP.md` ("Stub detection").
3. **Sampler params pinned** — `create_writer_agent` and
   `create_reviewer_agent` pass `temperature=0.6, top_p=0.95` to
   `OpenAIServerModel` so fw1 and fw2 endpoints behave identically
   regardless of what each llama-server has as `/props` defaults.
   `OpenAIServerModel`'s `**kwargs` flow into `self.kwargs` and are
   applied last in `_prepare_completion_kwargs`.

The writer-drift-modes log lives in
`memory/feedback_writer_agent_drift.md` (file I/O attempts, bash-as-
Python, speculative probing) for future drift-mode triage.

### [DONE] Reviewer criticizes chapter 1 for not having content we explicitly told it not to have

Observed across multiple chapter-1 (`README_internals.md`) runs. The
chapter is a tree-level overview with `scope_guard` and a custom
`sections` list specifically suppressing struct walkthroughs and code
excerpts. The reviewer's rubric (especially `source_coverage`) expects
those, and marks them missing.

**Status:** fixed. `build_review_prompt` now receives the per-chapter
section list AND the `scope_guard` text and threads both into the rubric
— Structure is evaluated against the chapter's declared sections (not
the full default set), and Source Coverage is scoped by the same guard
the writer obeys. Validation runs confirmed chapter 1 no longer fails
the rubric for missing struct walkthroughs that scope_guard explicitly
forbade.

### Source-usage telemetry (per-chapter summary done; cross-run aggregation still open)

Every chapter run logs every tool call (`read_freebsd_source`,
`search_books`, `directory_map`, `explore_tree`,
`resolve_c_definition`) inside the smolagents step output. As of
2026-04-30, `run_chapter` also prints a per-chapter banner
summarising the tool calls before returning.

A one-off aggregator across 8 historical logs (1,118 tool
invocations) showed a healthy mix:

- 50.7% `read_freebsd_source` (full file reads from FreeBSD tree)
- 21.8% `search_books` (books corpus)
- 18.5% `resolve_c_definition` (struct/function lookups in src)
- 7.2% `explore_tree` + 1.8% `directory_map` (directory discovery)

So ~78% of tool calls hit the FreeBSD source tree and ~22% hit the
books corpus — which matches the writer prompt's intent (books for
theory, source for facts). The aggregator also surfaced a real
defect: 7 calls of `resolve_c_definition(symbol="struct foo")` —
the writer literally copying the placeholder example from the
prompt instead of substituting a real symbol.

**Status of the original three ideas:**

1. **[DONE] Per-chapter source-usage summary** — shipped 2026-04-30.
   `_collect_tool_stats` regexes `step.code_action` text in
   `agent.memory.steps` after every `agent.run()` (must run inside
   `_run_agent`, before the next `reset=True` wipes memory).
   `_merge_tool_stats` accumulates across draft / review / revision /
   fact-fix stages keyed by stage name. `_format_stats_banner` prints
   the totals + per-tool top-N before `run_chapter` returns. Tool-call
   regexes live in `_STATS_TOOL_PATTERNS` — when adding a writer tool,
   add a pattern there too. See CODE_MAP.md "Track a new tool-use
   metric in the per-chapter banner."
2. **Cross-chapter aggregation** in `build_chapter_index` — *still
   open.* The per-chapter banner prints to stdout; nothing is persisted
   for cross-chapter analysis. Possible: write each chapter's stats
   dict to a sidecar JSON next to the README, then aggregate at index
   time. Useful for spotting pattern chapters that should be split, or
   chapters where the writer drifted off the source tree.
3. **Detect placeholder copying** as a writer-drift heuristic — *still
   open.* Any `resolve_c_definition(symbol="struct foo")` (or other
   prompt-example literal) is a near-certain sign the writer is
   following the prompt blindly. Either remove the literal example
   from the prompt or treat its appearance in tool calls as a
   regression signal in the new banner.

The historical aggregator script `source_stats.py` is now superseded
for per-chapter use by the pipeline banner; it can stay as the
prototype for cross-chapter aggregation (#2).

### [DONE] See Also block: duplication AND wrong relative-path depth

Two distinct bugs in `_add_see_also_links`, both shipped 2026-04-30.

**Bug 1: duplication.** Observed on `sys/vm/README.md` regenerated
2026-04-30 — the "See Also" section repeated the same three links
**four times**. Earlier commit `c13c560` ("strip prior See Also
auto-links before inserting (idempotency)") was meant to make the
injection idempotent but the strip predicate compared against an
exact-string set of expected targets and missed prior links whose
path shape differed.

**Bug 2: wrong relative-path depth.** Observed when a user clicked
a See Also link in `sys/kern/README_locking.md` pointing at
`../../../sys/vm/README_bcache.md` — that resolves to
`sys/kern/sys/vm/...`, a non-existent path. The link builder
computed `depth = len(target_dir.split("/")) + 1` (the *target's*
depth, not the path *from* the current file) and prepended that
many `../` to the full target path. Wrong vantage point. Affects
every cross-directory See Also link in the corpus.

**Status:** both fixed in `_add_see_also_links`:

1. Fresh links are now built with `os.path.relpath(target,
   start=current_dir)` — correct relative path regardless of either
   end's depth in the tree.
2. The strip-before-insert predicate widened: a list-item is treated
   as auto-inserted iff (a) its target's basename matches a known
   chapter README filename AND (b) `os.path.normpath(current_dir +
   target)` (with leading `../` segments stripped to handle paths
   that climb above the repo root) resolves to a known chapter file.
   This catches both the new correct form AND every legacy shape
   from the old buggy code, so a regen of any chapter cleanly
   replaces broken links with working ones.

Existing READMEs still have the broken paths baked in until each is
regenerated. A full corpus regen is queued for the next overnight run.

### Reviewer rubric doesn't penalize empty or contradictory Comparison sections

Observed on `sys/vm/README.md` (2026-04-30). The Comparison section
contains statements that pretend to contrast but don't:

> *"FreeBSD's `vm_map` is a red-black tree, while Linux uses a
> red-black tree."*

It also contains thinly-grounded cross-OS claims:

> *"NetBSD uses SLAB, while FreeBSD uses UMA."* (NetBSD's UVM is a
> substantially different design from FreeBSD's VM, not a SLAB-vs-UMA
> swap.)

The current reviewer rubric checks Accuracy against the FreeBSD source
tree (which it now has ground-truth injection for) but has no
criterion that fires on **comparison-section quality**: a statement
like "X uses Y while Z uses Y" passes Accuracy (both halves can be
true) without passing usefulness.

**Why it matters:** this is a content-quality issue, not a
fabrication issue, so none of the existing fact-check / symbol-catalog
machinery catches it. It also lowers user trust in the rest of the
chapter — a reader who notices the contradiction starts doubting
sections they can't independently verify.

**Possible fixes** (cheapest first):

1. **Add a Comparison-quality criterion to `build_review_prompt`.**
   Something like: *"In Comparison sections, every contrastive
   statement must identify a concrete difference. Statements of the
   form 'X uses Y while Z uses Y' (no actual contrast) or unsupported
   cross-OS claims must be flagged."* Reviewer is tool-less so it
   can't verify NetBSD's allocator, but it can catch the trivially
   self-contradicting form.
2. **Drop Comparison from the default section list** for chapters
   where the writer has no cross-OS grounding. The writer prompt
   currently asks for it on every chapter; making it opt-in via
   `chapters.yaml` would prevent the writer from filling space with
   weakly-grounded claims when it has nothing useful to say.
3. **Require a citation per comparison bullet** (book reference or
   source-tree path on the *other* OS, which we don't have). Not
   feasible without a non-FreeBSD source corpus, but listed for
   completeness.

**Status:** open. Option 1 is the obvious first step; Option 2 is
worth considering for chapters whose source surface is purely
FreeBSD-internal (most of them).

---

## What "improve coverage" can mean

There are several axes, and they pull in different directions. "More
chapters" is only one of them — and not necessarily the best.

- **Breadth (more topics).** Add chapters for GEOM, ZFS, capsicum,
  locking, KPI/KBI, sys/dev/, etc.
- **Depth (more detail per topic).** Each existing chapter splits into
  several focused sub-chapters (e.g., VM → vm_page / vm_map / pmap /
  fault-handling / UMA).
- **Granularity (per-directory).** Every meaningful directory in `sys/`
  gets a small README. Many small, very local.
- **Cross-cutting concerns (orthogonal patterns).** Locking, KPI/KBI,
  SYSCTL, capabilities, eventhandlers — patterns that thread through
  every subsystem. Not subsystems themselves.

These are not mutually exclusive, but they have very different
cost/value profiles.

---

## Tier 1 — clear wins, do these first

### 1. Add the missing big-rock subsystem chapters

Just append to `chapters.yaml`. The pipeline already works; this is
purely a "more entries in the YAML" exercise. Ranked by value:

| Chapter | Source | Status |
|---|---|---|
| **GEOM** | `sys/geom/` | **[DONE]** chapter 9 |
| **CAM** | `sys/cam/` | **[DONE]** chapter 10 |
| **ZFS** | `sys/contrib/openzfs/` | **[DONE]** chapter 13 |
| **VNET** | `sys/net/vnet.c` | **[DONE]** chapter 15 |
| **pf** | `sys/netpfil/pf/` | **[DONE]** chapter 16 |
| **ipfw + dummynet** | `sys/netpfil/ipfw/` | **[DONE]** chapter 17 |
| **NIC drivers (if_vr / iflib / if_em / if_cxgbe)** | `sys/dev/vr,e1000,cxgbe`, `sys/net/iflib.c` | **[DONE]** chapter 20 |
| **Capsicum** | `sys/kern/sys_capability.c` | **[DONE]** chapter 22 |
| **Locking primitives** | `sys/kern/kern_{mutex,rwlock,sx}.c`, `subr_{turnstile,sleepqueue,witness}.c` | **[DONE]** chapter 7 |
| **bhyve VMM** | `sys/amd64/vmm/` | **[DONE]** chapter 23 |
| **DTrace framework** | `sys/cddl/dev/dtrace/` | **[DONE]** chapter 24 |
| **Network 802.11** | `sys/net80211/` | open — well-isolated, often touched |
| **Kernel module loader** | `sys/kern/kern_linker.c`, `kern_module.c` | open — relevant any time someone writes a `.ko` |
| **netgraph** | `sys/netgraph/` | **[DONE]** chapter 25 (entry added; first generation pending) |
| **kqueue / event notification** | `sys/kern/kern_event.c` | open — distinctive FreeBSD event API |
| **TrustedBSD MAC framework** | `sys/security/mac/` | open — pluggable MAC, underpins parts of Capsicum |
| **Linuxulator** | `sys/compat/linux/` | open — Linux ABI emulation |

Phase A is now largely complete: 12 of the original 8 + 4 added chapters
landed (GEOM, CAM, ZFS, VNET, pf, ipfw, NIC drivers, Capsicum, Locking,
bhyve, DTrace, netgraph). The chapter set has grown from 13 to **25**.
Remaining candidates are listed above; pick whichever has demonstrated
user value.

### 2. Add cross-cutting / pattern chapters

Orthogonal to subsystems but high-value for coding agents. *Cheaper* to
write than subsystems because the source surface is small and the
topics are more self-contained.

| Chapter | Why it matters |
|---|---|
| **KPI vs KBI** | What the policy is, what counts as breaking, how to find KBI guarantees in the tree |
| **The SYSCTL framework** | Every kernel feature exposes itself this way |
| **Tunables, kernel options, and config(5)** | How `options FOO` flows from kernel config to compile |
| **Eventhandlers and SYSINIT/SYSUNINIT** | Already touched in the boot chapter; deserves its own |
| **The kernel `malloc` zoo** | UMA, malloc(9), free(9), pools — LLMs constantly mix up the contracts |

~5 chapters.

---

## Tier 2 — useful but more expensive

### 3. Per-directory mini-READMEs

Every non-trivial directory under `sys/` gets a 30–60-line README
explaining "what lives here, what's the entry point, what's the
relationship to the parent subsystem." So `sys/dev/iwlwifi/README.md`,
`sys/dev/usb/README.md`, etc.

**Caveats:**

- Per-directory cost is lower but the count is huge — `sys/dev/` alone
  has hundreds of directories.
- Reviewer/fact-check fidelity drops because each chapter's source
  surface is small (less for the reviewer to anchor on).

**A more disciplined version:** only directories that are
≥ ~10 source files AND are user-touchable (drivers being
written/modified, not internal helpers). That probably gets you to
~50 directories, which is plausible.

### 4. Deeper splits of existing chapters

For topics where the current single chapter is "too compressed."
VM is the prime candidate — split into vm_page / vm_object+vm_map /
pmap / vm_fault / UMA. Network stack could split into "input path,"
"output path," "socket layer," "TCP/UDP specifics," "if/ifnet."

**Caveats:**

- Invites duplication. Sub-chapters constantly cross-reference each
  other and overlap in scope-guard violations.
- The current `scope_guard` tooling helps but isn't perfect.
- Worth doing only if a single-chapter version has demonstrable gaps
  users hit.

---

## Tier 3 — probably not worth it

### 5. A chapter per kernel module
Diminishing returns; most modules are too small to warrant a full
chapter.

### 6. Auto-generated coverage from man9
Tempting (man9 has ~470 pages, all already structured), but man9 is
reference material and the READMEs are educational. Different shape,
different audience. Better to **cite** man9 from the READMEs (the
writer agent already has access to man9 via its corpus) than to derive
READMEs from man9.

---

## The non-obvious meta-point

The bigger lever isn't "more chapters" — it's **chapter discoverability
and per-directory navigation**. If an LLM working on `sys/dev/sound/`
doesn't know there's a useful chapter at `sys/README.md` *and* a
relevant pattern chapter on KPI/KBI elsewhere, the chapters might as
well not exist for that task.

Two cheap improvements that compound the value of any new chapters:

### A. Walk-up-the-tree links

Every leaf README should link to its **ancestor** READMEs ("subsystem
context: …"). The current navigation block shows siblings, not
ancestors. A reader at `sys/vm/README_bcache.md` should immediately see
links to `sys/vm/README.md` and `sys/README.md`.

### B. Per-directory pointer files

For directories without their own chapter, drop a one-line pointer
file: `→ See ../README.md for the parent subsystem`. Trivial to
generate, makes the chapter graph navigable from anywhere in the tree.

Both are tooling changes to `build_navigation` (and a small new
generator), not new content — and they'd raise the value of the
existing 13 chapters as much as adding 8 more would.

---

## Suggested execution plan

Each phase is independent — stop after any phase if it's enough.

### [LARGELY DONE] Phase A: Tier-1 subsystem chapters
12 of the originally-listed candidates landed (GEOM, CAM, ZFS, VNET,
pf, ipfw, NIC drivers, Capsicum, Locking, bhyve, DTrace, netgraph).
Open candidates: net80211, kernel module loader, kqueue, MAC framework,
Linuxulator — pick if/when they show demonstrated value.

### Phase B: Tier-1 pattern chapters (still open)
Append ~5 cross-cutting entries (KPI/KBI, SYSCTL, tunables/options,
eventhandlers/SYSINIT, kernel malloc zoo). Cheap because source
surface is small and topics are self-contained.

### Phase C: Navigation improvements (partially done)
- **`directory_map(path)` writer-agent tool** — *done.* Returns a
  structured one-level summary of a directory (subdirs, Makefile SRCS,
  per-file struct/function names, top-of-file purpose comments) so the
  writer can orient inside a directory in one tool call instead of
  reading every file individually. Documented in `CODE_MAP.md` and the
  README's "five agent tools" section.
- **Walk-up-the-tree links in `build_navigation`** — *still open.*
  Sibling links exist; ancestor links do not.
- **Per-directory pointer-file generator** — *still open.* Drop a
  one-line pointer `→ See ../README.md` in directories without their
  own chapter.

### Phase D (only if A–C reveal a real gap): per-directory mini-READMEs
Disciplined subset (~50 directories), with a separate, lighter-weight
pipeline — no full review/fact-check loop, just a directory-purpose
paragraph. Different prompt, different gate.

(An earlier proposal to ship pre-generated per-directory README files
into the FreeBSD tree was rejected in favor of `directory_map(path)`,
which computes the same structured view on demand from the current
source state — drift-free, zero file-generation maintenance.)

---

## Things to keep in mind

- **Always-read-the-source is the right default for LLM consumers.**
  The READMEs are orientation, not authority. Frame them that way in
  any user-facing message — including the README headers themselves
  if you want.
- **Keep the UNVERIFIED banner load-bearing.** Now that the criteria-
  only PASS gate works, that signal actually means something. Don't
  dilute it.
- **Version drift.** FreeBSD-CURRENT changes constantly. Plan for
  periodic regeneration; treat any chapter older than ~6 months with
  suspicion.
- **Coverage is asymptotic.** The kernel is too big to fully document.
  Stop adding chapters when the *next* one would have a small audience
  or low edit-frequency, even if it's intellectually appealing.
