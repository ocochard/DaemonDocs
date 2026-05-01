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

### [DONE — fix 1 shipped 2026-05-01] Struct field names and function-call mechanics not grounded in source; fact-check doesn't reach into struct bodies

Observed on the kernel-core chapter (`sys/README.md`, regenerated
2026-04-30, fw1-ch3.log). The writer described `struct sysinit` two
runs in a row with field names that **do not exist in
`sys/sys/kernel.h`**:

- Draft says: `{ const char *name; int si_sub; int si_order;
  sysinit_func_t si_func; ... }`
- Reality: `{ enum sysinit_sub_id subsystem; enum sysinit_elem_order
  order; STAILQ_ENTRY(sysinit) next; sysinit_cfunc_t func; const void
  *udata; }`

Every field name is wrong (extra `si_` prefix; `subsystem` →
`si_sub`; `order` → `si_order`; `func` → `si_func`; `udata` →
fictional `si_arg`). Every type is wrong (`int` instead of enum;
`sysinit_func_t` instead of `sysinit_cfunc_t`). The `STAILQ_ENTRY`
list linkage is missing entirely.

In the same run, the writer also invented a runtime `qsort(sysinit_set,
sysinit_set_size, sizeof(struct sysinit), sysinit_compar)` call. There
is no such call. The actual sort is `STAILQ_MERGESORT(list, NULL,
sysinit_compar, sysinit, next)` in `sys/kern/init_main.c:203`. The
*previous* run had it closer to right ("merge sort algorithm") and
the new run **regressed**, replacing a vague-but-correct claim with a
specific-but-fabricated one.

**Why the pipeline missed it:**

1. **Fact-check only verifies top-level symbol existence.** It checks
   that `struct sysinit` exists, that `mi_startup` exists, that
   `sysinit_compar` exists — and they all do. It does **not** parse
   `sys/sys/kernel.h` to compare the struct's actual field list
   against the field names the draft claims. So `si_sub`/`si_order`/
   `si_func` slip through because they look like field references,
   not symbols.
2. **Reviewer is tool-less.** It can't read `sys/sys/kernel.h` to
   confirm field names, so to it `int si_sub` and `enum sysinit_sub_id
   subsystem` are equally plausible. The reviewer's `accuracy: FAIL`
   in this run was firing on a *different* fact (`vm_init()` clearing
   `TDP_NOFAULTING`), not on the struct fields, and not on the
   fabricated `qsort` call.
3. **Writer paraphrases from training-data memory** when describing
   small data structures, despite the existing "quote-don't-paraphrase"
   prompt rule. The Authoritative Symbol Catalog confirms the *symbol*
   exists but does not give the writer the *field list*, so the writer
   fills the gap from memory.
4. **Cross-run instability.** Two runs of the same chapter produce
   substantively different (and differently wrong) descriptions of
   the same struct. This is the user-visible symptom: regenerating a
   chapter shouldn't change a struct definition.

**Why this is the top priority:** it directly defeats the chapter's
educational purpose. A reader who copies the `struct sysinit` example
into their own code, or who tries to `grep` the source for `si_sub`,
will hit a dead end and lose trust in the entire corpus. Unlike
prose-level imprecision (which a careful reader can route around),
fabricated struct fields look authoritative and propagate into reader
notes and code.

**Possible fixes** (in order of cost / impact):

1. **Strict struct-body fact-check.** When the draft contains a
   `struct <name> { ... }` code block, parse the cited header
   (`sys/sys/kernel.h`, etc.) and require every field name in the
   draft to appear as a field in the real struct. FAIL the chapter
   on any unknown field name. This is the lowest-cost change and
   would have caught both runs of the sysinit hallucination.
   Implementation: add a `_factcheck_struct_bodies` pass alongside
   the existing path/symbol verification in
   `_run_factcheck`/`fact_check_draft`.
2. **Force struct definitions to be verbatim quotes.** Tighten the
   writer prompt: any `struct X { ... }` block must be the result of
   `read_freebsd_source` on the defining header — not paraphrased.
   The writer can elide fields with `/* ... */` but cannot rename or
   retype them. Pair with (1) so the rule is enforced, not just
   requested.
3. **Inject struct field lists into the Authoritative Symbol
   Catalog.** The catalog already names the struct; extend
   `_resolve_c_definition` (or its catalog-feeding path) to include
   the field list for cited structs so the writer has the right
   answer in front of it instead of guessing.
4. **Function-call shape verification.** For function calls cited in
   prose ("`mi_startup` calls `qsort(...)`"), grep the source for the
   callee within a window of the caller. Catches the fabricated
   `qsort(sysinit_set, ...)` case. Higher false-positive risk —
   experiment after (1) lands.

**How to apply:** start with fix (1). It is local to fact-check, can
be tested against the existing `sys/README.md` regen as a fixture
(both runs would FAIL fact-check), and does not change writer
behavior. If (1) reduces the rate without eliminating it, add (2).

**Reproduction:** run `git diff sys/README.md` after the
2026-04-30 regen — both old and new versions show the same wrong
field names; the new version additionally fabricates the `qsort`
call. Ground truth is `sys/sys/kernel.h:struct sysinit` (5 fields:
`subsystem`, `order`, `next`, `func`, `udata`) and
`sys/kern/init_main.c:203` (`STAILQ_MERGESORT`, not `qsort`).

**What shipped (2026-05-01):** fix (1) — strict struct-body
fact-check. New helpers in the **Fact-checking** banner of
`generate-doc.py`:

- `_extract_struct_bodies(text)` finds `struct NAME { ... };`
  blocks inside fenced code blocks (inline-prose mentions are
  skipped) and parses the field list with
  `_parse_struct_fields`. The parser strips C comments, nested
  `{...}` regions (so an inline `enum { ... } p_state` reads as
  `p_state`), parenthesised macro arguments (so
  `STAILQ_ENTRY(sysinit) next` reads as `next`), bitfield widths,
  array shapes, and leading `*`s.
- `_real_struct_fields(name, src_root)` greps `sys/` for
  `struct NAME {`, reads the first matching header, and re-uses
  the same parser on the real body. Cached in
  `_STRUCT_FIELDS_CACHE` keyed by `(src_root, struct_name)`. An
  empty result means "verification unavailable" — silent skip,
  not "no fields" — to keep false positives at zero.
- `_verify_struct_bodies(claims, src_root)` cross-checks each
  claim against the real field set; structs whose name is
  already flagged by `_verify_structs` are skipped (don't
  double-report).
- Wired into `fact_check_draft` as a new `struct_fields_bogus`
  result key and into `_build_fact_check_prompt` with a writer
  instruction to read the defining header verbatim instead of
  paraphrasing.

Validated on the actual framework `sys/README.md` (the original
post-mortem fixture): the hallucinated body
`{ void (*func)(void *); void *data; int si_sub; int si_order;
const char *name; }` now FAILs fact-check with
`struct sysinit: void, data, si_sub, si_order, name` while a
verbatim-correct claim passes clean. Test harness:
`test_struct_factcheck.py`.

Fixes (2)–(4) — verbatim-quote enforcement, catalog-injected
field lists, and function-call shape verification — remain on
hold. Land them only if (1) proves insufficient on subsequent
runs.

---

### [DONE — shipped 2026-05-01] Mermaid flowchart: subgraph id collides with a node id, refuses to render with "would create a cycle"

Observed on the netgraph chapter (`sys/netgraph/README.md`,
2026-05-01). The diagram declared a `Userland` *node* and then
wrapped that same node in a `subgraph Userland`:

```mermaid
flowchart TD
    Userland["Userland (ngctl / libnetgraph)"] -->|...| NgSocket[...]
    ...
    subgraph Userland
        Userland
    end
```

Mermaid sees `subgraph Userland` and the `Userland` node living
inside it, tries to make the node a child of a subgraph with the
same id, and refuses to render with:

> Setting Userland as parent of Userland would create a cycle

Node ids and subgraph ids share one namespace in mermaid
flowcharts; this is the same shape of trap as the prior C-syntax
leak in classDiagram bodies (commit `1860fd6`), but for a
different diagram type and a different parsing rule.

**Why the existing prompt rules missed it:** the writer-prompt
flowchart hint covered diagram-shape basics but said nothing
about the node/subgraph namespace. The reviewer's Mermaid
criterion is a tool-less check ("correct keywords, no missing
brackets, proper arrows") and can't actually run the renderer to
catch a semantic clash.

**Possible fixes** (in order of cost / impact):

1. **Deterministic post-process sanitizer** that detects a
   subgraph id which also appears as a node id and renames the
   subgraph (e.g. `Userland` → `Userland_grp`), preserving the
   original id as the visible title. Robustness floor: catches
   every regression on every regen, regardless of model
   compliance. Same shape as the JSONDecodeError fallback in
   `_extract_json`.
2. **Writer-prompt rule** explaining that node ids and subgraph
   ids share a namespace, with the canonical fix
   (`subgraph UserlandGroup ["Userland"]`). Reduces the rate but
   the model can ignore it under pressure.
3. **Tool-using reviewer that actually invokes the mermaid CLI**
   to render the diagram and surface render errors. Heaviest;
   requires installing `mmdc` and a headless browser on every
   endpoint. Skip until (1)+(2) prove insufficient.

**What shipped (2026-05-01):** fixes (1) AND (2) together.

- New `_sanitize_mermaid_flowchart(block_body)` and
  `_sanitize_mermaid_blocks(text)` helpers in `generate-doc.py`,
  source-ordered just before `_extract_json` in the
  **Orchestrator** banner. Detect collisions by collecting
  `subgraph <id>` lines and a superset of node ids from the
  rest of the diagram, then rename only the colliding
  subgraph header. Idempotent; non-flowchart mermaid blocks
  pass through unchanged. Wired into `run_chapter` between the
  fact-fix step and the H1 prepend.
- Writer-prompt flowchart hint in `build_chapter_prompt`
  extended with the namespace rule and the
  `subgraph UserlandGroup ["Userland"]` example.
- The currently-broken `sys/netgraph/README.md` on framework
  was patched in place by running the new sanitizer against
  the file. Future regens of any chapter exhibiting this shape
  will be cleaned automatically.

**Reproduction:** open the original file in any Mermaid renderer
(GitHub web view, mermaid live editor); the diagram displays
"Setting Userland as parent of Userland would create a cycle"
and refuses to render. Test harness: `test_mermaid_sanitizer.py`
(16 sub-checks, includes an end-to-end test against the actual
`sys/netgraph/README.md`).

---

### [DONE — shipped 2026-05-01] Broken `.md` cross-chapter links survived the See-Also fix; sanitizer added

Observed on `sys/kern/README_locking.md` (2026-05-01): the See
Also section pointed at `vm/README_bcache.md`, which from
`sys/kern/` resolves to `sys/kern/vm/README_bcache.md` — a path
that does not exist. The real chapter is `sys/vm/README_bcache.md`.
A corpus-wide audit found **22 broken `.md` links** across 6
chapters, all the same legacy "as-if-living-at-sys/" shape.

The earlier post-mortem ("See Also block: wrong relative-path
depth", shipped 2026-04-30) fixed *fresh* link generation in
`_add_see_also_links` but only the strip-before-insert filter
was supposed to clean up legacy links. That filter checks
``os.path.normpath(current_dir + target)`` against the chapter
file set: a stale link like `vm/README_bcache.md` from
`sys/kern/` joins to `sys/kern/vm/README_bcache.md`, which is
**not** a chapter file, so the filter kept it.

**What shipped:** a deterministic post-process sanitizer
`_sanitize_chapter_links(content, current_file, chapter_files)`
in `generate-doc.py`, source-ordered right after
`_sanitize_mermaid_blocks` in the **Orchestrator** banner.
Same robustness-floor pattern as the mermaid sanitizer:

1. For every `[label](*.md[#anchor])` link, resolve the target
   against the file's directory.
2. If it lands on a real chapter file → leave alone.
3. Else, look for a unique chapter file matching by:
   (a) trailing two path components (e.g. `vm/README.md`
   uniquely identifies `sys/vm/README.md`), then
   (b) bare basename. If a unique match exists → rewrite to the
   correct relative path, preserving any `#anchor`.
4. No unique match: drop the entire list-item line. Inline-prose
   links are left alone (deleting just the link would mangle the
   sentence).

A second pass dedupes exact-duplicate `- [label](target)` lines
inside the See Also section; rewriting two stylistically
different broken links to the same target left doubled entries
that `_add_see_also_links` strip didn't catch.

Wired into `build_navigation` (right after
`_add_see_also_links`) so it runs every nav rebuild AND every
fresh chapter run. The 6 dirty corpus files were patched in
place: 19 links rewritten, 11 list-items dropped (3 pointed at
chapters that don't exist; 8 were post-rewrite duplicates).
Final state: **25/25 chapters** clean for both mermaid and link
sanitizers, zero broken `.md` links anywhere in the corpus.

**Why the tail-2 disambiguator matters:** the corpus has many
`README.md` files (one per per-directory chapter). Pure basename
matching for `vm/README.md` from `sys/kern/` would find 5
candidates and have to drop the line. Tail-2 narrows that to one
(`sys/vm/README.md`) and rescues the link. Bare `README.md`
without a parent hint stays ambiguous and gets dropped — correct
behaviour; we won't guess.

Test harness: `test_link_sanitizer.py` (18 sub-checks: the
real-corpus bug, idempotence, ambiguous targets, anchor
preservation, inline-prose-vs-list-item handling, dedup, and
end-to-end against the actual on-disk corpus).

---

### [DONE] Reviewer emits JSON with unescaped inner quotes; `_extract_json` raises and kills the chapter

Observed on the buffer cache chapter (`sys/vm/README_bcache.md`,
mac-ch8.log, 2026-05-01 00:32). Reviewer was mid-revision-loop with
sensible criteria scores (most PASS, two real FAILs to fix). Its
reply contained string values like:

```
"completeness": "PASS: ... discussion of "background processing" and ..."
```

The unescaped inner double quotes close the string early, the next
comma is no longer inside a string, and `json.loads` raises
`JSONDecodeError: Expecting ',' delimiter`. `_extract_json` lets the
exception propagate; `run_chapter` doesn't catch it; `main` exits
rc=1. **The chapter is lost** — no file written, the in-progress
draft is discarded, the runner moves on to the next queue entry.

Crucially, ch8 was on track to converge: `comparison_quality: PASS`,
`rationale: PASS`, only `accuracy: FAIL` (real `struct buflists`
issue) and `no_marketing: FAIL` ("integrates tightly"). The
revision loop would likely have produced an acceptable draft. The
crash threw away that progress for a syntactic reason.

**Why this matters:** unlike the struct-grounding issue, this is
not a quality problem — it's a robustness problem. Any chapter
whose reviewer happens to use quoted phrases inside criterion
strings can be killed mid-loop with no fallback. The best-draft
tracking elsewhere in the pipeline doesn't help here because
`_extract_json` raises *before* the criteria are read into
`best_draft`.

**Possible fixes** (in order of cost / impact):

1. **Catch `JSONDecodeError` in `run_chapter` and fall back to the
   last successfully-parsed review.** If round 2 parses clean and
   round 3 raises, treat round 3 as "no signal" and write the
   round-2-best draft with UNVERIFIED banner. Cheapest, no prompt
   change, no parsing heuristics. Implementation: wrap the
   `_extract_json(review_raw)` call in try/except, set `review_json
   = None` on failure, skip the criteria-update path, continue the
   loop.
2. **Sanitize before parsing.** Run a pre-pass that escapes inner
   quotes inside string values. Easy to get wrong (regex on JSON is
   notoriously fragile), but catches more than just this case.
3. **Tighten the reviewer prompt.** Add: "Inside any string value,
   use single quotes or backticks for inline phrases — never inner
   double quotes." Cheap, but reviewers occasionally ignore prompt
   rules under pressure (we've seen this before with the "no
   hedging" rule).

**How to apply:** ship (1) immediately as a robustness floor — it
costs nothing and prevents the lost-chapter outcome regardless of
what the reviewer does. Pair with (3) to reduce frequency. Skip
(2) — too brittle.

**Reproduction:** see mac-ch8.log around line tail-60. The
JSONDecodeError points to "line 5 column 88" — the inner quotes
inside the `completeness` value.

**What shipped (2026-05-01):** fix (1) landed in `_extract_json`.
The brace-matched-substring path now wraps `json.loads` in
try/except and returns `None` on `JSONDecodeError`/`ValueError`
instead of raising. The caller in `run_chapter` (line ~3417)
already had a parse-retry-once path for `None`, so a malformed
reviewer reply now triggers one retry and (if it still fails)
breaks the loop with the chapter UNVERIFIED — instead of
propagating to `main` and exiting rc=1 with no file written.
Behavior change is contained: the function used to raise on the
recovery path and returns None on the first-attempt path; it now
returns None on both. The single caller already handles None.

Fix (3) — tightening the reviewer prompt to forbid inner double
quotes — not yet shipped. Land if (1) alone proves insufficient.

---

### [DONE] LLM HTTP calls have no inactivity timeout; a wedged endpoint stalls the chapter indefinitely

Observed 2026-05-01 on the first ch8 re-run after the JSONDecodeError
fix. Pattern:

- Writer mid-step (had just issued a `read_freebsd_source` call).
- Log file frozen at a fixed mtime for 6+ minutes.
- Python process state `S` (sleeping). CPU time well below wall-clock
  time — process was idle, not stuck-busy.
- llama-server at 0.0% CPU, `/health` returning `{"status":"ok"}`.
- Two python→llama TCP connections still ESTABLISHED.

Neither side was making progress, neither side was going to time out.
The python `openai` SDK has no inactivity timeout by default — it
will wait on a half-open connection forever. We had to kill the
chapter manually.

**Why this matters:** even with the JSONDecodeError fix, a single
wedged HTTP call can lose a chapter. The previous overnight run had
4–5 chapters that ran 2+ hours; we cannot tell from logs alone
whether any of them were partially stalled (just slow generation
vs. wedged-then-recovered). The lack of a timeout means a transient
network or server hiccup turns into a permanent stall.

**Possible fixes** (in order of cost / impact):

1. **Set `client_kwargs={"timeout": N}` on `OpenAIServerModel`.**
   smolagents forwards `client_kwargs` to `openai.OpenAI()`, which
   accepts a `timeout` parameter. On timeout the SDK raises
   `APITimeoutError`; `run_chapter` already wraps each `_run_agent`
   call in `try/except Exception`, so the existing recovery path
   kicks in and the chapter writes UNVERIFIED instead of stalling.
   600s gives ~5× headroom over typical step duration (30–120s)
   while bounding the worst case.
2. **Per-call timeout passed via the agent**. More invasive — would
   need smolagents support for per-completion timeout. Not currently
   available in the API. Skip.
3. **External watchdog** that kills the python process if the log
   stops growing for N minutes. Heavier ops infrastructure; only
   worth it if (1) proves insufficient (e.g. the SDK doesn't enforce
   read-timeout reliably).

**What shipped (2026-05-01):**

- Added `"timeout"` key to `MODEL_CONFIG` (default 600s, override via
  `DAEMONDOCS_LLM_TIMEOUT` env var).
- Both `create_writer_agent` and `create_reviewer_agent` now pass
  `client_kwargs={"timeout": MODEL_CONFIG["timeout"]}` to
  `OpenAIServerModel`.
- The OpenAI SDK enforces this as a read timeout on every HTTP call
  — wedged streams now raise `APITimeoutError` after 10 min of
  silence, the existing try/except in `run_chapter` catches it,
  the review loop breaks and the best draft so far is written
  UNVERIFIED. No code change needed in `run_chapter` because the
  failure path was already there for general agent exceptions.

**Reproduction:** the original stall was on
`/tmp/regen-queue/fw1-ch8.log` after 07:48:47 — log mtime frozen,
process state `S`, CPU time 6m05s vs 14m21s wall-clock, llama-server
idle at 0% CPU but `/health` ok. Two ESTABLISHED TCP connections to
:8080 with no traffic.

---

### [DONE — shipped 2026-05-01] Queueing strategy assumes endpoints are equal speed; largest-first is wrong when one endpoint is much slower

Observed in the 2026-04-30 → 2026-05-01 overnight run (25 chapters,
3 endpoints, largest-first queue). Per-endpoint stats parsed from
runner logs:

| Endpoint | Chapters | Total | Avg/ch | Min | Max |
|---|---|---|---|---|---|
| fw1 (localhost) | 10 | 631 min | **63 min** | 18 | 131 |
| fw2 (LAN, similar GPU) | 10 | 607 min | **61 min** | 19 | 120 |
| mac (LAN, slower GPU) | 4 (+1 crash) | 556 min | **139 min** | 110 | 167 |

Three findings:

1. **fw1 and fw2 are statistically tied.** Same average within
   noise, same chapter count, same workload. Treat them as
   interchangeable.
2. **mac is ~2.2× slower per chapter, uniformly.** Its *fastest*
   chapter (110 min) is nearly twice the median for fw1/fw2. Not
   tail latency — the endpoint is just slower across the board.
3. **Largest-first ordering routes the worst work to the worst
   endpoint.** The runners pop from a shared queue head-first;
   whichever endpoint becomes free claims the next-largest pending
   chapter. mac happened to be free when ch17 (110 min on mac),
   ch13 (112 min), ch7 (167 min), ch2 (167 min) came up — all
   long chapters. If those had landed on fw1/fw2, they'd have
   taken ~half as long.

**Why this matters:** the run finished at 06:39, ~10h31m after
launch. mac contributed 4 chapters in roughly the time fw1 and
fw2 each contributed 10. Dropping mac entirely would yield ~13h
wall-clock on fw1+fw2 alone (25 chapters / 2 endpoints × 63 min
≈ 13h) — only ~25% slower, despite removing one of three
workers. mac's *positive* contribution to throughput is small;
its *risk* of becoming a long-tail limiter is large.

**Possible fixes** (in order of cost / impact):

1. **Smallest-first for the slow endpoint.** Have mac's runner
   pop from the *tail* of the queue while fw1/fw2 pop from the
   head. Largest-first is still correct for the fast endpoints
   (avoids long stragglers), but mac should pick up the short
   chapters that wouldn't dominate its slot anyway. Change
   isolated to `runner.sh` (or queue-launcher script that splits
   by label).
2. **Per-endpoint speed annotation in the queue.** Tag each
   chapter with an estimated cost (small/medium/large) and have
   each runner claim from a tier appropriate to its speed.
   Heavier infrastructure than (1); only worth it if endpoint
   count grows beyond 3.
3. **Drop mac for the next overnight.** Use only fw1+fw2. Wall
   clock goes from ~10.5h to ~13h, but the run becomes more
   predictable and the failure surface shrinks. Worth doing once
   while investigating whether mac's slowness is fixable at the
   model-server level (smaller GPU? different `n_ctx`? CPU
   offload?).
4. **Investigate mac at the endpoint level.** 2.2× is a large
   gap; some of it may be config (model size, quantization,
   `n_threads`). If mac can be brought to within ~30% of fw1/fw2,
   re-introduce it with strategy (1).

**How to apply:** ship (1) before the next full-corpus run — it's
a one-line `sed` change to `runner.sh` (use `tail -1` instead of
`head -1` for the mac launcher's pop), no code change in
`generate-doc.py`. Pair with (4) when you have time to look at
the llama-server config on mac. Defer (2) until there's a third
fast endpoint to justify the bookkeeping.

**What shipped (2026-05-01):** option (1).

`runner.sh` now takes a third positional arg `pop_end` —
`head` (default, fast endpoints) or `tail` (slow endpoint).
Implementation lives at `framework:/tmp/regen-queue/runner.sh`.
The pop is still a single `lockf`-serialised `sh -c` block; the
sed address is passed as a positional `$1` to the inner shell so
we never embed a literal `$` inside a double-quoted sed
expression. The first draft did, and BSD sh expanded `"$d"` to
the empty string — the queue's last line became undeletable and
the slow runner looped forever on the final chapter. The
positional-arg form sidesteps that quoting trap entirely.

A companion `start-runners.sh` (in the same directory) records
the convention: fw1/fw2 launch with `head`, mac with `tail`.
Each runner runs under `daemon(8)` so an SSH disconnect doesn't
kill it (consistent with the project memory on FreeBSD
fire-and-forget background jobs).

End-to-end test on a 1..20 synthetic queue (2 concurrent runners,
one head + one tail): 20 unique chapters popped, 0 duplicates,
fast got 1..10 in order, slow got 20..11 in order.

Backward-compatible: existing invocations of `runner.sh LABEL URL`
without a third arg still work and behave identically (default
`head`).

**Reproduction:** parse `/tmp/regen-queue/{fw1,fw2,mac}.log`
timestamps; the per-endpoint averages above came from a Python
script over the runner-emitted "starting chapter X" /
"chapter X finished" lines.

**Open follow-ups (deferred, not shipped here):**
- (2) per-chapter cost annotations in the queue — defer until a
  third fast endpoint exists.
- (3) drop mac entirely — moot now that (1) prevents long-tail
  domination.
- (4) investigate mac's llama-server config (model size,
  quantization, `n_threads`) to close the 2.2× gap. Worth doing
  before the next full-corpus run.

---

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
endpoint on 2026-04-28. Same model (`Qwen3.6-35B-A3B-UD-Q4_K_XL`) as
the primary endpoint, but slightly different sampling defaults exposed
by `/props`.

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
   `OpenAIServerModel` so all llama-server endpoints behave identically
   regardless of what each one has as `/props` defaults.
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

### [DONE] Reviewer rubric doesn't penalize empty or contradictory Comparison sections

Observed on `sys/vm/README.md` (2026-04-30). The Comparison section
contained statements that pretended to contrast but didn't:

> *"FreeBSD's `vm_map` is a red-black tree, while Linux uses a
> red-black tree."*

It also contained thinly-grounded cross-OS claims:

> *"NetBSD uses SLAB, while FreeBSD uses UMA."* (NetBSD's UVM is a
> substantially different design from FreeBSD's VM, not a SLAB-vs-UMA
> swap.)

The reviewer's Accuracy criterion checks against the FreeBSD source
tree (with ground-truth injection for paths/symbols/macros) but had
no criterion that fired on **comparison-section quality**: a statement
like "X uses Y while Z uses Y" passes Accuracy (both halves can be
true) without passing usefulness.

**Status:** fixed via two complementary changes shipped 2026-04-30.

**Change 1: opt-out via `chapters.yaml` (Option B).** The pre-existing
per-chapter `sections:` mechanism already supports dropping any
section. Two chapters whose source surface is purely FreeBSD-internal
with no real cross-OS analog at this level of detail now opt out:

- **Build System — buildworld and buildkernel** (chapter 4) — Linux
  Kbuild and NetBSD's bsd.*.mk are real systems but a meaningful
  contrast would be its own essay, not a paragraph.
- **netgraph — Graph-based Networking Framework** (chapter 25) —
  Linux nftables/tc and OpenBSD divert(4) solve narrower,
  differently-shaped problems; no useful one-paragraph contrast.

Source Tree (chapter 1) was already opted out via its tree-overview
sections list. Total chapters opting out of Comparison: 3 of 25.

**Change 2: Comparison-Quality criterion (Option A).** New criterion
9 in `build_review_prompt`, gated on `"Comparison" in sections` (same
shape as the existing `wants_diagram` Mermaid gate). Chapters that
opted out auto-pass with `"comparison_quality": "PASS: not required"`.
Chapters that kept Comparison get graded on three explicit failure
shapes:

- **Tautology** — "FreeBSD uses X while Linux also uses X."
- **Unsupported cross-OS claim** — assertions about Linux/macOS/NetBSD
  internals stated as fact with no concrete differentiator.
- **Vague contrast** — "Linux handles this differently" with no
  specifics.

The reviewer has no cross-OS source corpus, so this is shape-matching
against the draft, not fact-checking against external sources. Both
example sentences from the post-mortem above are FAIL by this rule.

**Why both changes:** Option B alone leaves chapters that *do* opt in
unchecked (the writer can still emit weak Comparisons there). Option
A alone still pays Comparison-generation cost on chapters that have
nothing useful to say. Together: B prevents the "fill space" failure
mode upstream by not asking; A catches residual weak content on
chapters that legitimately keep Comparison.

The third option from the original plan — *"Require a citation per
comparison bullet (book reference or non-FreeBSD source path)"* —
remains infeasible without a non-FreeBSD source corpus and is
unlikely to ship.

### Struct-snippet faithfulness — code blocks can disagree with prose, nothing checks the layout

Observed on `sys/sys/README_mbuf.md` (chapter 26, generated 2026-05-01,
shipped CLEAN with reviewer 9/9 PASS). The chapter's prose correctly
describes the mbuf chain semantics, the `M_PKTHDR` / `M_EXT` /
`M_EXTPG` discriminant, the ref-counted cluster mechanism via `m_ext`,
and the `m_tag` linked list. But the rendered ` ```c struct mbuf {...}``` `
block is wildly wrong:

```c
/* What the chapter shipped */
struct mbuf {
    union {
        struct  pkthdr pkthdr;  /* M_PKTHDR set */
        char    *mext;          /* M_EXT set */
    } m_hdr;
};
```

vs. the real `sys/sys/mbuf.h:329`, which has:

- Two leading chain-pointer unions (`m_next`/`m_slist`/`m_stailq` and
  `m_nextpkt`/`m_slistpkt`/`m_stailqpkt`) — the *defining* feature of
  an mbuf, completely absent from the snippet.
- The bookkeeping fields `m_data`, `m_len`, and the `m_type:8 / m_flags:24`
  bitfield — also absent.
- A 4-way trailing union covering `M_PKTHDR` (`struct pkthdr`),
  `M_EXTPG` (multi-page TLS bookkeeping: `m_epg_npgs`, `m_epg_tls`,
  `m_epg_so`, …), `M_EXT` (`struct m_ext` — *not* a bare `char *`),
  and inline payload — collapsed in the chapter to a 2-arm union with
  the wrong types.
- The bogus outer name `} m_hdr;` (no such member exists in the real
  struct; the trailing union is anonymous).

**Why this slipped through:**

- **Fact-check verifies symbol existence, not layout fidelity.**
  `_verify_structs` confirms `struct mbuf` exists in the tree.
  `_verify_struct_fields` only flags fields the writer cited that
  *don't* exist — the inverse error (omitting all the real fields and
  inventing a wrong outer shape) is silent.
- **Reviewer reads the prose.** The prose is correct. The reviewer's
  Accuracy criterion is satisfied because the *narrative* matches the
  source. Nothing currently looks for prose↔code-block consistency.
- **Writer paraphrased from training-data memory** rather than quoting
  what `read_freebsd_source` would return. The "quote-don't-paraphrase"
  rule in the writer prompt exists but is advisory; for the largest
  structs the writer is likely to abbreviate "for clarity" and end up
  with something that looks plausible but isn't load-bearing.

This is a pure-LLM-confabulation failure mode that the existing
defenses don't reach. It's most dangerous on structs that are
**well-known but have evolved** — `struct mbuf`, `struct buf`,
`struct vnode`, `struct proc`, `struct pcb` — where the writer's
training-data prior is strong enough to override the actual source.

**Possible fixes (none picked yet):**

1. **Real-struct injection diff.** For every ` ```c struct NAME { ... }``` `
   block in the draft, find the actual definition in the source tree
   (we already have `resolve_c_definition` for this), compute a
   coarse-grained diff (member-name set, basic shape), and feed
   *both* to the reviewer with a new criterion: "does the code block
   match the source layout, or is the gap explained as an excerpt?"
   The reviewer doesn't need byte-for-byte equality — chapters
   legitimately abbreviate — but it needs to flag missing top-level
   fields like `m_next` / `m_data`.
2. **Structural shape verifier.** Extract the field-name set from
   each emitted struct block, intersect with the field-name set from
   the in-tree definition, and demote to UNVERIFIED when the
   intersection is below some threshold *and* the chapter doesn't
   explicitly mark the block as "abbreviated" / "simplified". This
   needs no LLM call, only the existing struct-extraction regex.
3. **Force quote-only mode for big-rock structs.** A per-chapter
   knob in `chapters.yaml` (say `verbatim_structs: [mbuf, m_ext, pkthdr]`)
   that adds a hard rule to the writer prompt: these structs MUST be
   pasted verbatim from `read_freebsd_source`, no abbreviation.
   Cheaper than (1) but only protects the structs we anticipate.

Option (2) is the cheapest by far and would have caught the mbuf
case (the rendered block has 1 field name in common with the real
definition: `pkthdr` — and even that is wrong as a member name).
It also has zero false-positive risk for chapters that don't
include code blocks at all. Worth prototyping next.

**Status:** open. Filed 2026-05-01 after ch26 mbuf shipped clean
with the wrong struct.

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

#### Networking deep-dive chapters (gated on struct-body fact-check)

The Network Stack chapter (ch15, `sys/net/README.md`) is an end-to-end
*packet-flow tour* — recv path, send path, netisr, socket buffers — but
it deliberately does not deep-dive into mbuf, TCP/UDP, or the IP layer
because each of those is too dense to fit alongside the integration
narrative. Three chapters worth adding, in this order of value:

| Chapter | Why it matters | Source |
|---|---|---|
| **mbuf — Network Buffer Allocation and Chaining** | Foundational; cited from every networking chapter; covers `m_get`/`m_getcl`/`m_pullup`/`m_copydata`, `m_next` vs `m_nextpkt`, `pkthdr`, `m_ext` clusters, mbuf zones, `m_tag` send/receive tags, exhaustion behavior. Currently every other networking chapter hand-waves over this. | `sys/sys/mbuf.h`, `sys/kern/uipc_mbuf.c`, `sys/kern/uipc_mbuf2.c`, `sys/kern/subr_mbpool.c` |
| **Transport Protocols — inpcb, tcpcb, TCP State Machine, UDP, Modular Stacks** | The FreeBSD-distinctive transport story: `struct inpcb`/`struct tcpcb` pair, inp hash tables, pluggable congestion control (`cc_newreno`/`cc_cubic`/`cc_dctcp`/`cc_rack`), TCP timers, syncache vs SYN cookies, modular TCP stacks (`tcp_stacks/`, RACK, BBR). Generic protocol description belongs in textbooks; this chapter focuses on what FreeBSD does differently. | `sys/netinet/in_pcb.c`, `sys/netinet/tcp_*.c`, `sys/netinet/udp_*.c`, `sys/netinet/cc/`, `sys/netinet/tcp_stacks/` |
| **IP Layer — IPv4, IPv6, Forwarding, FIB, and nhop** | One chapter covering what's *common* between v4 and v6 (forwarding, fragmentation, multicast) with v4-vs-v6 as a comparison axis. The `nhop` / `fib` rewrite is the central architectural story — more interesting than rehashing two protocols separately. | `sys/netinet/ip_input.c`, `sys/netinet/ip_output.c`, `sys/netinet6/`, `sys/net/route/` |

**Why:** mbufs and transport PCBs are the two biggest current gaps in
networking depth — ch15 names them but doesn't explain them. The IP
chapter consolidates v4+v6 around the FIB/nhop rewrite, which is the
genuinely novel architectural change worth documenting.

**How to apply — gate lifted 2026-05-01.** Networking chapters are
exactly where the `struct sysinit`-style hallucination hurts most:
`struct mbuf`, `struct ip`, `struct tcphdr`, `struct tcpcb`,
`struct inpcb` are dense, frequently cited, and stable across decades
of training-data history — the worst-case combination for the writer
to paraphrase from memory instead of quoting. A reader who
copy-pastes a fabricated `struct mbuf` and then can't compile against
`sys/sys/mbuf.h` is the failure these chapters most need to avoid.
The struct-body fact-check (DONE entry above) now flags exactly that
shape, so adding these three is unblocked. Run them once each and
inspect the per-chapter banner for any `struct_fields_bogus` issues
before declaring success.

UDP does **not** get its own chapter — it's small enough to fit as a
section inside the Transport Protocols chapter alongside the inpcb
machinery. A standalone UDP chapter would mostly be padding.

IPv4 and IPv6 are kept in **one** chapter rather than split. Splitting
them produces too much overlap (forwarding, FIB integration, multicast)
and hides the v4-vs-v6 contrast that makes the differences memorable.

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

### A. [DONE] Walk-up-the-tree links

Every leaf README links to its **ancestor** READMEs as a closest-first
breadcrumb in the auto-generated nav sidebar. A reader at
`sys/vm/README_bcache.md` sees:

> **Up:** [Virtual Memory Subsystem — vm_page, UMA, and Pagers](README.md) ▸
> [Kernel Core — Structure and Entry Point](../README.md) ▸
> [Source Tree — Layout and Conventions](../../README_internals.md)

Implementation: `_build_ancestor_chain(current_file, all_files)` in
`generate-doc.py` returns ancestor chapter files closest-first using
two rules — same-directory plain `README.md` (e.g. `sys/vm/README.md`
is an ancestor of `sys/vm/README_bcache.md`) plus any chapter whose
output_file lives in a strict-prefix directory of the current file's
directory. `build_navigation` joins them into the `Up:` line of the
sidebar with `os.path.relpath` for correct relative paths. Verified
live across the corpus on framework.

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
- **Walk-up-the-tree links in `build_navigation`** — *done.*
  `_build_ancestor_chain` + the `Up:` breadcrumb in the sidebar.
  Verified across the corpus 2026-05-01. See "Walk-up-the-tree
  links" entry above.
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
