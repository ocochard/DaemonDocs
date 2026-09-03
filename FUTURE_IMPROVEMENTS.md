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

### [DONE — shipped 2026-09-02] The struct verifier picked the biggest homonym and called it authoritative, so ch4 was told its correct fields were hallucinated

**Symptom.** ch4 (build system) finished `rc=0` in 12781s but shipped
UNVERIFIED. Its fact-check raised five issues; on inspection **all of
them were false positives**, and the writer said so:

> Conclusion: All four flagged issues are false positives from a
> fact-checking [pass] ... introduce hallucinations into a correct
> FreeBSD chapter.

The writer was right. It refused the fact-fix and shipped correct prose.

**What the fact-check claimed, and the truth.**

| Claim | Reality |
|---|---|
| `struct cfgfile`, `struct file_list` not in tree | Both at `usr.sbin/config/config.h:97,103` |
| `struct device` fields `d_done, d_name, yyfile, d_next` don't exist | All four verbatim from `usr.sbin/config/config.h:139` |
| `struct device` body has 0 overlap with real definition | Same cause |
| Kernel option `FOO` not in `options*`/`NOTES` | Metasyntactic placeholder quoted from the chapter's own subject file, `share/mk/src.opts.mk` (`WITH_FOO`, `MK_FOO`) |

**Two independent root causes.**

1. **Coverage gap.** ch4 documents `config(8)` but `usr.sbin/config`
   was not in its `source_dirs` (only `share/mk`, `tools/build`). The
   real definitions were outside the search roots entirely.

2. **Silent wrong-winner (the dangerous one).** The tree holds **six**
   `struct device` definitions. `_real_struct_fields` selected by
   **max field count**, so linuxkpi's 22-field struct
   (`sys/compat/linuxkpi/common/include/linux/device.h`) beat
   config(8)'s 4-field one. Selecting by size is exactly backwards for
   the case that matters: a small canonical struct against a large
   unrelated homonym.

**Why this was worse than ch21.** ch21 failed *open* — fact-fix
stubbed, the banner went up, the damage was visible. This fails
*closed*. The "carry the ANSWER" design (see CODE_MAP) handed the
writer the wrong field list as ground truth and told it
`Do NOT re-derive them by reading the header`. Had the writer
complied, correct prose would have been rewritten into wrong prose
that every downstream stage would then certify as clean — a
hallucination *manufactured by the hallucination detector*. Only the
writer's refusal prevented it, and refusal is not a mechanism we can
rely on.

**Fix.** `_real_struct_fields` now collects every *distinct* parsed
definition instead of the largest. When candidates disagree it logs
the competing paths and returns empty — "verification unavailable",
which callers already treat as don't-flag. Unambiguous structs are
unaffected: `vm_page` still resolves to its 16 fields. Also added
`usr.sbin/config` to ch4's `source_dirs`, which fixes claim 1
independently.

Ambiguity is not a rare edge: `device`, `buf`, `file`, `node` and
friends recur across `sys/`, `usr.sbin/`, and the compat shims. This
was mis-verifying silently for every chapter that touched one.

**Not fixed: the `FOO` class.** The kernel-option verifier cannot tell
an option name from a metasyntactic placeholder in quoted
documentation. Narrow and low-harm (the writer dismissed it), but it
means option findings inside quoted `share/mk` prose are unreliable.
The general shape — *verifiers cannot see quoting context* — is the
same gap that makes prose-level claims unverifiable (roadmap step 4).

**A third defect, found while fixing the second: burial.** The
candidate loop only opens `candidates[:32]`. `struct thread` matches
**1012 files** under `sys/` (nearly every kernel source takes a
`struct thread *td` parameter), and the real `sys/sys/proc.h` sorted
to **rank 39** — losing on alphabetical order within the canonical-
header tier to `_rmlock.h`, `acct.h`, `alq.h`. It was never opened, so
`struct thread` silently returned "verification unavailable" for every
thread/process chapter. `struct proc` was buried the same way.

Fixed with a second grep for the *definition shape*
(`struct NAME {`, or `struct NAME` at end-of-line for K&R) whose hits
sort ahead of mere mentions, so real definitions enter the slice
regardless of filename. Falls back to the original ordering on
grep failure or timeout. `thread` now resolves to 127 fields, `proc`
to 100.

**A fourth defect, same struct, found in ch8's output (2026-09-02):
`#define` field aliases.** The fact-check flagged `struct
thread->td_retval` as a nonexistent field. It exists — as a macro
alias onto a nested union member:

```c
/* sys/sys/proc.h:365 */
#define td_retval    td_uretoff.tdu_retval
```

~95 files under `sys/` spell the field that way; `td_uretoff.tdu_retval`
is how nobody writes it. `_parse_struct_fields` reads only
`;`-terminated declarators inside the struct body, so alias `#define`s
were invisible to all three verification layers at once.

This one is *not* a near-miss. The ch4 defect was caught by the
writer's refusal; here the writer **complied**. `README_process.md`
shipped with the `td_retval` prose deleted, `grep -c UNVERIFIED` = 0,
and every later stage certifying it clean. Third confirmed instance of
the same root shape: **the verifier's model of C is narrower than C**
(struct homonyms, the `FOO` placeholder class, now macro aliases).

**Fix.** `_struct_field_aliases` harvests object-like `#define`s from
the winning definition's file and unions them into the returned field
set — placed in `_real_struct_fields` because all three layers share
it, so one change repairs every consumer. Admission rule: the
replacement text must begin with an identifier the struct really
declares. That is the load-bearing constraint — `proc.h` alone holds
**326** `#define`s, and admitting them wholesale would convert the
field verifier into a rubber stamp certifying any capitalised noise as
a field. Verified no leakage of `TDF_BORROWING`, `PROC_LOCK`,
`TD_IS_RUNNING`, etc. Both `.` and `->` alias forms are handled
(`b_object` is `b_bufobj->bo_object`); aliases-onto-aliases resolve by
fixed point (`m_epg_startcopy` → `m_epg_npgs`).

Deltas are small enough to audit by eye, which is the point — a fix
here that doubled a field count would be indistinguishable from a
rubber stamp:

| struct | before | after | admitted |
|---|---|---|---|
| `thread` | 127 | 133 | `td_retval`, `td_siglist`, `td_start/endzero`, `td_start/endcopy` |
| `proc` | 100 | 107 | `p_pgid`, `p_session`, `p_siglist`, … |
| `mbuf` | 28 | 33 | `m_epg_pa`, `m_epg_hdr`, `m_epg_trail`, … |
| `vnode` | 35 | 38 | `v_type`, `v_state`, `v_object` |
| `buf` | 43 | 45 | `b_error`, `b_object` |
| `vm_page`, `socket` | 16, 58 | unchanged | — |

**Accepted imprecision.** `td_startzero`/`td_endzero` are bcopy-range
markers aliased onto `td_flags`/`td_sigmask`, not field names a reader
would write. They are structurally indistinguishable from real
aliases, and admitting them fails *open* (a rarely-cited name goes
unflagged) — the correct direction for this verifier. No heuristic
added to exclude them.

Guard: `tests/test_struct_define_alias.py`, which pins the literal
ch8 string `struct thread->td_retval` as not-flagged and asserts an
invented field is still caught. Confirmed to fail on pre-fix code.

**That prefilter caused a regression, repaired in the same change.**
Promoting definition sites pulled `sys/netpfil/ipfw/test/dn_test.h` —
dummynet's two-field *fake mbuf* — into the slice, which made the real
`sys/sys/mbuf.h` look ambiguous and turned off verification for ch36's
core struct. Test-path definitions (`/test/`, `/tests/`, `/testsuite/`,
`/regress/`) are now excluded from the ambiguity determination: a stub
is not a competing authority. `mbuf` resolves to 28 fields again.

Worth noting the interaction — the ambiguity check and the burial fix
pull against each other. Surfacing more true definitions also surfaces
more false rivals, so widening candidate discovery *requires* the stub
exclusion or it converts silent-wrong-answers into silent-no-answers.

**Accepted coverage loss.** `struct resource` now skips verification:
`sys/compat/linuxkpi/common/include/linux/ioport.h` (4 fields) and
`sys/sys/rman.h` genuinely disagree. Max-field-count previously picked
rman.h correctly *by luck*. Skipping is the right trade — the ch4
failure mode is worse than no check — but it is a real loss for ch23.

**Guard.** `tests/test_struct_ambiguity.py` (14 checks), verified
against pre-change code: the ambiguity checks fail with the exact
bogus message ch4 received, and the burial checks fail on `thread`.

### [DONE — shipped 2026-09-01] A redundant `|\n` in the declaration regex was exponential, and the hang detector's dump blamed the wrong code

The rate floor from the entry below paid for itself the day it shipped:
ch38 and ch34 both produced stack dumps that the old `second > first`
probe would have suppressed. ch34's was a genuine stalled `recv`. ch38's
pointed here, and the first reading was wrong.

**The dump's deepest worker frame** was `re/__init__.py:177 in search`,
called from `ResolveCDefinition.forward`, so the obvious suspect was the
tool's unbounded `os.walk` over `sys/` — ~15200 files, ~357 MB, up to
three `re.search` passes each, no timeout, while the fact-check greps
next door have had `_GREP_TIMEOUT_SEC` since ch13. Measured: **16.2s**
for a full tree scan. Real, but not a 1808s wedge. The walk was a red
herring; a single instantaneous sample lands inside `re.search` most of
the time precisely *because* that is where the loop spends itself.

**The actual defect** was in `_FUNC_DECL_RE`, whose argument-list groups
were `(?:[^{;=()]|\n)*?`. A negated character class already matches
newline — it is not DOTALL-dependent — so the `|\n` branch was pure
redundancy, and every newline in the argument list had two ways to
match: 2^n paths across n lines. On `sys/arm64/arm64/vfp.c`, an
unremarkable 32 KB file, `_extract_func_sigs` did not terminate.

| | before | after |
|---|---|---|
| `_extract_func_sigs(vfp.c)` | >25s, unbounded | **0.002s**, 47 sigs |
| identical output, 2500-file sample of `sys/` | — | **2500/2500** |
| declaration classes still resolved | — | all (groups 4) |

The trigger is ordinary code: an `if (` a few lines above a function
definition. The `(?!return|goto|...|if|...)` guard excludes `if` only at
the *start* of a line, and the match begins earlier.

**Two rejected alternatives, both measured.**

1. **A length bound on the arg list.** `{0,400}` terminates but
   silently drops real declarations with long argument lists — four in
   `sys/dev/pms/RefTisa/sallsdk/spc/saproto.h`, whose parameter lists
   run ~470 chars. `{0,800}` reintroduces the hang. There is no cap that
   both terminates and keeps every real match, so the cap would have
   been load-bearing *and* lossy. The alternation itself is the defect.
2. **An atomic group on the identifier run**
   (`(?=(?P<a>...))(?P=a)`), by analogy with the
   `_extract_fenced_function_defs` fix. Did not help: stripping the
   pattern down showed the run was innocent — a single-iteration variant
   still took 4.9s, while replacing the newline alternation alone took
   it to 0.000s.

**Also fixed, and independent of the hang:** the three walks now share
one deadline (`_RESOLVE_WALK_BUDGET_SEC`, env
`DAEMONDOCS_RESOLVE_BUDGET_SEC`, default 45s, `0` disables). This is a
**seatbelt, not the fix** — the same relation the scan budget in
`_extract_fenced_function_defs` has to its atomic group. It checks
between files and cannot interrupt one pathological match; `re` has no
timeout.

Two things about it are easy to get wrong, and I got the first one wrong
before measuring:

- **The default must exceed a complete negative search.** A search that
  legitimately finds nothing costs **23.0s** (`cc_record_rtt`,
  `cc_newround`, `prx` all 22.8-23.0s uncapped). My first default was
  20s, which converted every genuine "Could not find definition" into
  "budget exceeded" — a correct answer degraded into an unresolved one.
  45s leaves ~2x headroom.
- **A truncated search must not claim absence.** "Could not find
  definition" is a positive claim the writer turns into prose, and the
  fact-checker grades the prose, not the provenance of the string. On
  truncation the tool now says it was cut short and is explicitly *not*
  evidence of absence. Same failure class as a verifier that silently
  skips a claim class: a false clean is worse than no check.

The old "limit" in the fallback walk deserves naming: `for fname in
files[:50]  # Limit to avoid slow walks`. It bounded nothing — all 3315
directories were still visited — while making **2872 of 15221 source
files (19%)**, in the 133 directories holding more than 50 entries,
permanently invisible. A symbol defined only in `sys/dev/ath` or
`sys/contrib` could report "not found" while present.

Regression tests in `tests/test_resolve_budget.py` (22 checks). Group 2
uses `SIGALRM`, because a regression test for a hang must **fail** rather
than hang — the first draft wedged the suite when the fix was reverted,
which is a test that cannot report the bug it exists for. The revert arm
produces 6 named FAILs and exits 1. Group 3 pins the redundancy claim
itself (20k random strings over `a;=(){}\n\t *,\\` with zero language
difference) so nobody reintroduces the newline branch for readability.

**What this does not fix.** The writer called
`resolve_c_definition(symbol='prx')` — a three-character local variable
that cannot be a kernel symbol and costs a full 23s tree scan to
disprove. Both this regex and the walk budget make a wedge legible and
bounded; neither curbs what the writer asks for. Curbing that is still
open.

**Correction (2026-09-01), and a trap worth naming.** An earlier version
of this paragraph said ch38's "input grew 12k → 934k tokens over 31
steps, ~40k per step" and treated that as unbounded context growth. That
reading is wrong. **The `Input tokens:` field in a smolagents step line is
a RUNNING TOTAL across the run, not the size of that call's prompt.** The
per-call prompt is the step-to-step *delta*: on ch38 it went 11.9k →
44.4k over 31 steps, and on ch5's draft retry 12.7k → 42.0k over 52 —
ordinary ReAct history accumulation, nowhere near the 131072-token
context. Nothing was growing pathologically.

Two further claims died with it. Tool observations are not the driver:
across ch5's 52 draft steps every `Out:` block totals 4,518 chars
(~1.1k tokens), about 1% of the transcript. And the slow steps are not
slow from prefill — decode holds a flat ~15 tok/s throughout.

What actually costs the wall clock is **per-step output volume**, the
same distribution the 2026-08-23 `WRITER_MAX_TOKENS` entry documented on
ch3. ch5's draft retry: step 52 generated 14,377 tokens in 955s (24% of
a 3,910s draft), step 51 another 7,783 in 478s; the top 8 of 52 steps are
64% of the draft. ch38's step 31 is the same shape — 881s for 13,196
tokens. Step 52's 14,377 sits within 2k of the 16384 default cap and
never truncated, so the cap is working as designed rather than failing.

Before quoting a token figure from a step line, difference it. The
cumulative number is ~20x the real prompt by the end of a long draft and
makes healthy runs look like runaways.

### [DONE — shipped 2026-09-01] A decode trickle read as liveness, so every hang guard suppressed itself and ch7 died undiagnosed

ch7 (VM) was killed by the runner's wall-clock watchdog after 3861s. The
useful part is what did *not* happen: no stack dump, no `APITimeoutError`,
no UNVERIFIED write. Three independent guards were armed and all three
stayed quiet through 2481s of total client silence.

Measured on the live endpoint while the chapter was wedged:

| signal | value | healthy |
|---|---|---|
| `n_decode_total` | +2 per 20s (0.1/s) | ~5-7/s |
| `tokens_predicted_total` | frozen at 18929 | advancing |
| server decode time / wall-clock | 1216s / 3861s (36%) | most of it |
| prefill cache hit | 92% | — |
| client log growth | none for 41 min | steady |

**Root cause: `_endpoint_is_decoding()` returned `second > first`.** Bare
movement counted as a working model. At 0.1 decode/s the endpoint is dead
for practical purposes but technically advancing, so:

- the **hang detector** (1800s) suppressed its dump — it asks the endpoint
  first, by design, to avoid the 2026-08-23 false positive;
- the **runner watchdog** logged "endpoint still decoding; NOT killing" on
  earlier chapters for the same reason, and on ch7 only fired because its
  wall-clock rule is unconditional;
- the **httpx read timeout** (600s) never tripped, because a single-float
  timeout resets on every byte received and a trickle keeps resetting it.

**The fix** is a rate floor, not a new timeout: `_DECODE_MIN_RATE`
(env `DAEMONDOCS_DECODE_MIN_RATE`, default 1.0/s) — ~5x below observed
healthy throughput and 10x above the trickle, so it separates the two
without re-introducing the false positive. The probe keeps its fail-safe
direction: metrics unavailable, flat counter, counter reset, or trickle
all read as "not decoding" so the detector still dumps.

**Two fixes considered and rejected.** An explicit
`httpx.Timeout(connect=, read=, write=, pool=)` would not have caught
ch7 either — bytes *were* arriving, just barely, so no read gap ever
opened. And lowering the runner watchdog would trade this failure for the
2026-08-23 false-dump problem. The defect was never the threshold; it was
treating a boolean as a rate.

**What this does not fix.** ch7's writer was at 332k input tokens by step
17, growing ~20-28k per step with output flat. The rate floor makes the
next occurrence *legible* (a stack dump naming the phase) rather than
silent; it does not curb the writer's context appetite, which is a
separate open item.

Regression tests in `tests/test_regex_hang.py` group 4: ch7's exact
numbers (2 decodes in 20s) must read as NOT decoding, healthy ~5/s must
read as decoding, and flat / counter-reset must stay caught. The gap is
patched via `time.sleep`, not by shrinking `_DECODE_PROBE_GAP_SEC` —
the rate is `delta / gap`, so a tiny gap inflates the rate and silently
defeats the trickle case (that mistake cost a test-authoring round).

### [DONE — shipped 2026-08-31] The writer's symbol tool matched definitions only, so header-only KPIs were unconfirmable and the reviewer flagged correct prose as hallucinated

**Symptom.** During the 2026-08-31 regen, both endpoints stalled on the
same class of problem. ch37 (transport) logged four consecutive failed
lookups — `cc_conn_init`, `cc_post_recovery`, `cc_after_idle`,
`cc_ecnpkt_handler` — each answered "No exact definition found for X,
but it appears in these files". ch39 (sound) cited `getptr()` and the
reviewer FAILed criterion 1 (Accuracy) on it across two consecutive
review rounds; the revision did not remove it, because the symbol is
real and the writer had no way to prove it.

**Cause.** `_extract_func_sigs` had a single regex, `func_def_re`,
requiring a trailing `{`. That matches function *definitions* only.
Three real, citable symbol classes have no body anywhere in the tree:

1. **Prototypes** — `void\tcc_conn_init(struct tcpcb *tp);` in
   `sys/netinet/tcp_var.h`. Every KPI exposed only through a header.
2. **Function-pointer struct members** — both the inline spelling
   `int (*sv_fetch_syscall_args)(struct thread *);` and the typedef'd
   `pgo_getpages_t\t\t*pgo_getpages;` (literal tabs, no parenthesis).
3. **kobj interface methods** — declared in `.m` interface files, not
   C: `METHOD uint32_t getptr { ... } DEFAULT channel_nogetptr;`.
   Drivers realize them as `KOBJMETHOD(channel_getptr, foo_getptr)`
   table entries, so the bare interface name has no C definition at
   all. `ResolveCDefinition`'s file walks filtered to `.c`/`.h`, so the
   147 `.m` files in the tree were invisible to it outright.

**Fix.** Added `_FUNC_DECL_RE` (three alternatives, one per bodyless
shape, ending in `;`) alongside the existing definition regex, and
`_extract_kobj_methods` for `.m` files, with `.m` added to both file
walks and a dispatch branch in the extraction loop. Shared the keyword
stoplist as `_C_KEYWORD_STOPWORDS`.

**Two false-positive traps hit while building it** — both are pinned by
tests, do not reintroduce them:

- `re.DOTALL` on the declaration regex let `[^{;]*?` cross statement
  boundaries, so `else if (sbt != 0)\n\trval = sleepq_timedwait(...)`
  parsed as a declaration of `rval`. Dropped DOTALL; excluded `=`.
- `return chn_resizebuf(c, latency, -1, 0);` parsed as return-type
  `return` declaring `chn_resizebuf`. Fixed with a negative lookahead
  on statement keywords. A five-file sweep went 5 suspicious matches
  to 0.

**What was NOT changed, deliberately.** `cc_record_rtt`, `cc_rttsample`
and `cc_newround` — three of the seven ch37 lookups — do not exist
anywhere in `sys/`. The tool's "Could not find definition" was correct
for those; they are writer hallucinations and the reviewer was right to
flag them. A looser matcher that resolved them would be a regression,
not a fix. `tests/test_resolve_declarations.py` group 4 pins this.

**Scope note.** This is the writer-side tool. The *verifier*-side
equivalent for function-pointer members shipped separately in
`2bbed64`; the two code paths are independent and both were needed.

Tests: `tests/test_resolve_declarations.py` (28 checks, 18 fail pre-fix).

### [DONE — shipped 2026-08-30] Struct verifier recognised one of three definition spellings; real structs verified as "missing"

Third instance of the same failure family as the ch34 grep cap and the
ch40 extractors, found while ch37 was still running. ch37 (TCP) was
graded **FAIL on Accuracy** for this:

> The draft cites two structs that are on the MISSING list as if they
> were real FreeBSD entities. (1) `struct in_endpoints` is presented in
> the Key Data Structures code block as part of the real struct
> in_conninfo in sys/netinet/in_pcb.h.

`struct in_endpoints` **is** real, exactly where the draft said it was.
Only the second name in that finding (`struct tcp_stat`, whose real
spelling is `tcpstat`) was a genuine hallucination — so the reviewer
was half right and the chapter burned revision rounds on the half that
was not.

Stage 2 of `_batched_grep_present` filters stage 1's fixed-string hits
down to definition-shaped lines, so the 1 MB output cap holds
definitions instead of the forest of pointer-typed uses. Whatever
stage 2 drops, stage 3 never sees, and the symbol is reported missing.
The filter was `^struct [A-Za-z_]\w* *\{`. FreeBSD writes struct
definitions four ways, and that matched one:

| spelling | distinct tags in `sys/` | examples |
|---|---|---|
| `struct foo {` | (matched) | most of the tree |
| `struct<TAB>foo {` | 42 | `arphdr`, `icmpstat`, `ether_arp`, `direct`, `eui64`, `fork_req`, `tcpstat` |
| indented (nested in a struct/union) | 442 | `in_endpoints` in `in_conninfo`; most `fw_*` / `mt7915_*` driver headers |
| `typedef struct foo {` | 3735 | `ksiginfo`, `moduledata`, `elf_file`, `if_txrx`, `__sigset` |

**The `\t` trap.** The obvious fix — write `[ \t]` — does not work and
looks like it does. `shape_grep` is consumed by BSD `grep -E`, where a
bracket expression does **not** interpret `\t`: `[ \t]` matches a
space, a backslash, or the letter `t`, never a tab. The pattern must
carry a literal tab byte. Worse, the failure is invisible to the
obvious test, because Python's `re` *does* understand `\t` — so a unit
test over `pattern_template` passes while the pipeline stays broken.
`tests/test_struct_shape_grep.py` group 1 therefore captures the
pattern `_verify_structs` actually hands to grep and asserts a real tab
byte is in it. I hit this trap during the fix: the first patch used
`[ \t]` inside a raw string, tested green by hand (I typed an actual
tab at the shell), and left the verifier just as blind.

**Why relaxing the anchor is safe.** Leading whitespace and the
`typedef` prefix are permitted only on the alternative that ends in
`{`. A pointer use or parameter declaration never ends in a brace —
verified over the whole tree — and `typedef struct NAME;` forward
declarations with a brace do not exist. The K&R alternative
(`struct foo` at end of line, brace on the next) keeps its `^` anchor
for precisely this reason: unanchored, an indented `struct thread *td`
parameter would pass it and the verifier would start rubber-stamping.

**Measured effect.** Across the 55 shipped chapters, 464 struct claims
now yield 18 reported missing, and spot-checking those 18 found the
remaining ones to be genuine (`buflists`, `pglist`, `ifnets`,
`db_command_table`, `sysctl_ctx_list` have no `struct NAME {` anywhere
— they are field names, list heads and macro artifacts the writer
described as structs).

**Note on the arithmetic:** the false-positive rate this removes is not
"3735 + 442 + 42 structs were broken" — it is that *any chapter citing
a struct in one of those spellings* got a fabricated Accuracy failure.
ch37 is the observed instance; how many earlier chapters lost rounds to
it was not measured and is not recoverable from the logs.

**Not fixed:** ch37 itself. Its verdict cache is in-process, so the
running job kept the poisoned verdicts; the chapter would need a re-run
to be graded fairly. `_verify_functions` was checked for the same
blindness and does **not** share it: its shape filter is not anchored
the same way, and 12 real functions across the tree (`tcp_input`,
`uma_zalloc`, `m_getm2`, `bus_alloc_resource`, `tcp_newtcpcb`, ...)
verify while 3 fabricated near-misses are still flagged. Its
leading-column handling is deliberate per the comment there.

### [DONE — shipped 2026-08-30] The extractors invented symbol claims, and the reviewer dutifully failed the chapter over them

ch40 (USB/Thunderbolt) shipped UNVERIFIED, scoring 5/8 → 7/8 → 6/8 and
regressing on the final round. Its Accuracy failure listed these as
symbols the draft claimed but source did not define:

```
Missing structs:   above, defines, tree
Missing functions: newbus, usb, usbdi
```

The draft never claimed any of the six. They came out of the
extractors, from two distinct shapes.

**`struct` used as an English noun.** The prose was "building a
`tb_cfg_read` frame (the struct above)", "packs the same fields the
on-the-wire struct defines", and "a four-struct tree". The pattern was
`\bstruct\s+([a-zA-Z_]\w*)\b`, which cannot tell the C keyword from
the ordinary noun. The third case is worse than the other two: `\b`
matches *inside* `four-struct`, so a hyphenated English compound
produced the struct name `tree`. A sweep of all 56 shipped chapters
found three more live instances — `avoids`, `allocated`, `rather`.

**Man-page citations read as function calls.** `usbdi(9)`, `usb(4)`
and `newbus(4)` came from the chapter's own See Also list. `name(N)`
is shape-identical to a zero- or one-argument call, so the function
extractor had no way to reject them.

The mechanism is the same one as ch34's grep-cap defect, one layer
upstream: **the reviewer was not wrong.** Its rubric says to FAIL
Accuracy when claimed symbols do not exist, and it was handed six
symbols that do not exist. Every layer downstream behaved correctly on
poisoned input. That is why the fix belongs in the extractor and not
in the rubric, and why "make the reviewer more forgiving" would have
been exactly the wrong response — it would have taught the reviewer to
ignore real hallucinations too.

**Fix.** `_ENGLISH_AFTER_STRUCT`, a verified denylist of words that
follow `struct` in prose, plus a `(?<![\w-])` guard replacing `\b` so
neither `four-struct` nor `sub_struct` matches. `_MANPAGE_CITATION_RE`
tests the *paren contents* for a bare man-page section, which keeps
`free(9)` (citation) and `free(ptr)` (call) on opposite sides.

**The denylist is the dangerous part of this fix.** FreeBSD names many
real types with ordinary English words — `struct buf`, `struct file`,
`struct proc`, `struct thread`, `struct mount`, `struct link`,
`struct name`. Adding any of those to the list would blind the
fact-checker to a real type, which is a strictly worse failure than
the one being fixed: a false positive costs a review round, a false
*negative* ships a hallucination as verified. So no word goes on the
list without `grep -rw "struct <word> {" ~/freebsd-src/sys` returning
nothing first, and group 4 of `tests/test_extractor_english.py`
re-verifies the entire list against `sys/` on every run rather than
trusting the comment.

Two things were deliberately *not* done:

- **`acked()`, `idle()`, `destroy()`, `recovery()` were left alone.**
  These showed up as pseudo-calls in README_transport prose and were
  tempting denylist entries. But `idle` and `loss` have real
  definition-shaped hits in `sys/`, and more importantly the writer
  writing `` `acked()` `` for a thing that is not a function *is* a
  drafting defect the fact-checker should report. Group 6 of the test
  suite pins that decision so nobody tidies them into the ignore list.
- **The reviewer's `max_steps=5` truncation was not touched.** ch40's
  round 2 hit the cap, which is a plausible second cause of the 7/8 →
  6/8 regression and is independent of this defect. The cap was
  lowered from 15 to 5 on 2026-08-23 to stop runaway reviewers; moving
  it back without measurement would trade one failure mode for the
  other.

Verified against real data, not just synthetic cases: on the actual
ch40 draft all six false positives are gone and 23 genuine structs
still extract; across all 56 shipped chapters the struct count fell
607 → 604 (exactly the three residual words), and every one of the 27
remaining inflection-looking names is a real FreeBSD tag (`ucred`,
`termios`, `sigacts`, `vfsops`, `witness`, `timehands`, …).

**Not fixed by this:** ch40's UNVERIFIED status. The extractor no
longer manufactures the claims, but the chapter was graded under the
old behavior and would need a re-run to clear.

### [PARTIALLY DONE — step 1 shipped 2026-08-27] Jargon goes undefined: every verifier asks "is this symbol real?", none asks "will the reader understand it?"

Reported by the user against `sys/vm/README_bcache.md` (ch12):

> The `BIO_UNMAPPED` flag indicates that the data pages are not mapped
> into KVM and must be mapped before the I/O can proceed.

`KVM` appears five times in that chapter and is never defined. The text
also never answers the obvious follow-up — *why do both modes exist?* It
states a mechanism ("must be mapped before the I/O can proceed") and
omits the point: unmapped I/O exists so a device can DMA straight from
physical pages without spending KVM and pmap/TLB work. KVM is finite on
32-bit and contended even on amd64.

Worse, line 253 of that chapter **invented** a rationale:

> If they are not (for example, because the pageout daemon has paged them
> out), the buffer must be "imported" back into KVM

Wrong. An unmapped buffer is unmapped because the caller asked for
`BIO_UNMAPPED`, not because the pagedaemon evicted anything. This is the
key insight from the post-mortem: **undefined jargon and confabulated
rationale are the same defect.** The writer treated "the flag exists" as
sufficient, never engaged with why two modes exist, and when the reviewer
pushed for rationale it produced something plausible instead of something
true. Any fix that only adds definitions leaves the confabulation intact.

**Why the existing mechanisms did not catch it.** Two already existed:

- A per-chapter `Glossary` section in `_SECTION_CATALOG`. Opt-in, and
  **1 of 40 chapters uses it** (the VM chapter). Only 2 chapters override
  `sections` at all, so the opt-in is effectively dead.
- Reviewer criterion 8, "Rationale", which asks almost exactly the user's
  question: "does it explain WHY the design exists — what engineering
  problem it solves". It graded ch12 PASS.

Criterion 8 fails structurally, not by accident:

1. **Its trigger examples are VM-flavoured** — shadow chains, inactive
   queues, UMA kegs vs zones, copy-on-write, witness, turnstiles, NUMA
   domains. Nothing cues the reviewer to treat a *flag* as a non-obvious
   mechanism, and the model pattern-matches the examples it was given.
2. **It is one binary verdict over a ~25 KB draft.** Find rationale for
   three big things and five undefined terms ride along inside one PASS.
   There is no per-term accounting.
3. **It asks the model to simulate a junior reader.** A model that knows
   what KVM is does not *experience* `KVM` as needing a definition. This
   is the same reason the reviewer cannot be trusted to guess whether a
   path exists — and the fix is the same: compute it in Python and hand
   over a list it cannot rationalize away.

#### Step 1 — jargon-density check in Python [SHIPPED 2026-08-27]

`_extract_unglossed_jargon` and `_extract_unexpanded_acronyms`, wired into
`fact_check_draft` and surfaced to the reviewer as an "Undefined Terms
Detected" block. Two independent scans:

- **Curated terms** (`_JARGON_TERMS`): a hand-maintained set of
  FreeBSD/OS-internals terms needing a gloss on first prose use. Curated
  deliberately — a generic "hard word" heuristic drowns the writer.
- **Unexpanded acronyms**: any `[A-Z]{2,}` used 2+ times that is never
  expanded anywhere in the chapter. Safety net for terms missing from the
  curated list, which will always lag the source tree.

Both mask fenced code blocks: a term inside a struct definition or DTrace
script is not prose use, and demanding a gloss there would be wrong.

Findings go to `jargon_unglossed` / `acronyms_unexpanded` and are
**deliberately excluded from `total_issues`**. That count gates the
fact-fix loop, whose prompt is about deleting hallucinated symbols. An
undefined term is not a false claim, and folding it in would (a) fire that
loop on nearly every chapter and (b) blend "delete this lie" with "explain
this term" in one instruction — the prompt-blending this document already
records as backfiring. Readability goes to the reviewer, which owns
accessibility (criterion 5) and rationale (criterion 8).

**Two calibration lessons, both found by testing against real chapters
rather than synthetic drafts:**

*The gloss cue must be adjacent, not nearby.* The first version accepted a
bare `(` or `:` anywhere within 400 characters. It passed every synthetic
test and **missed ch12** — an unrelated parenthetical 90 characters after
`KVM` counted as its definition. Cues are now two tiers: verbal cues
("stands for", "is a") anywhere in the window; punctuation cues only
immediately after the term, with a gloss body of 2+ words that is not a
cross-reference ("see below").

*False-positive rate is the whole ballgame.* Measured across all 31
shipped chapters, then tightened twice:

| | findings/chapter |
|---|---|
| first cut | 10.4 |
| after dropping `sysctl`, `README`, TCP state names, TX/RX, chip models | 7.5 |
| after dropping bare `vfs`/`zone`, SCSI command words, queue macros, arch names | **7.0** |

Range is now 2-13. `vfs` and `zone` came out of the curated list as too
generic ("the vfs layer" is ordinary prose; `zone` collides with UMA, jail
and DNS zones). What survives reads as genuine: `VMCS`/`VMX`/`SVM`/`GPA`
really are unexplained in the vmm chapter, `BBR`/`RACK`/`ECN` really are
undefined TCP algorithm names, and `UMA`/`newbus`/`devclass`/`sysinit`/`kld`
genuinely go undefined across many chapters.

Tests: `tests/test_jargon_gloss.py` — 9 groups, including a regression
test that asserts the real ch12 text still flags `KVM` (the case the loose
cue regex missed) and one asserting readability findings stay out of
`total_issues`.

#### Step 2 — make Glossary a default section, and link terms to it [SHIPPED 2026-08-29]

Add `Glossary` to `_DEFAULT_SECTIONS`, positioned immediately after
`Quick Summary`. The catalog comment already warns that defining terms
*after* `Architecture` has used them defeats the point. Populate it from
step 1's output so entries are grounded in terms the chapter actually
uses rather than invented.

Then — the piece the user asked about directly — **a deterministic
first-use linker**, a fourth sibling to `_link_see_also_source_paths` and
`_link_manpage_refs` in phase 4. Pure Python, no LLM:

- in-chapter: wrap first prose use as `[KVM](#glossary)`;
- cross-chapter: for terms defined in another chapter's glossary, link to
  `../../<that-chapter>#kvm`, reusing the existing relative-depth logic
  (note the ch17 depth bug: depth must be computed from the final
  `output_file`, not assumed);
- idempotent, and skipping any term already inside a link or backticks —
  the same rules `_link_manpage_refs` already follows.

Step 1 does not do this and cannot: linking to a glossary requires the
glossary to exist, and today 1 of 40 chapters has one. Sequencing is
therefore step 1 (define terms inline) → step 2 (glossary exists) →
linker.

Cost: one extra section per chapter, and the benefit only materialises on
a full regeneration — so it lands with the next full run, not
retroactively.

**What shipped**, and three ways it differs from the spec above:

- `Glossary` is `_DEFAULT_SECTIONS[1]`, right after `Quick Summary`.
  Default chapters go from 7 H2 sections to 8; the reviewer's Structure
  criterion already interpolates `{section_count}` from the list, so no
  rubric text was hard-coded and none needed changing.
- `_link_glossary_first_use` is the fourth phase-4 linker, plus
  `_parse_glossary_entries` / `_find_glossary_span` /
  `_GLOSSARY_ANCHOR`. A pre-pass over all chapters builds the
  cross-chapter term index before the rewrite loop, so a chapter
  processed early can still link to a definition in one processed late.
  Tests in `tests/test_glossary_linker.py`.

1. **Per-term anchors do not exist.** The spec's two bullets contradicted
   each other — `#glossary` for in-chapter, `#kvm` for cross-chapter —
   and the `#kvm` form is the wrong one. GitHub mints anchors for
   *headings* only; a glossary term is a bolded list item, so `#slab`
   matches nothing and the reader lands at the top of the file with no
   error anywhere. The first implementation emitted per-term anchors and
   a corpus dry run showed all of them dead. Everything now targets the
   Glossary section. Pinned by test 9, which asserts every emitted
   anchor matches a real heading in the target file.
2. **Two skips the spec did not list**, both found by dry-running phase 4
   over the real corpus rather than by reasoning about inputs: `See Also`
   (already a link list owned by three other linkers — an inline link
   inside an existing bullet is noise) and double-quoted spans (`a "drop
   zone"` in `README_internals.md` is ordinary English, not the UMA
   term; it was the corpus's one homograph false positive). Together
   these removed 4 of 15 links from the dry run.
3. **Idempotency needs a term-keyed guard.** "Link the first prose use"
   is not "link the first *unlinked* use": on a second pass the existing
   link is skipped as inside-a-link and the loop links the next mention
   instead, accumulating one link per run. The guard keys on the term,
   not the target, because every term in a chapter now shares the single
   `#glossary` target.

Dry run over the 149-README corpus: 11 links across 7 chapters, every
one resolving to a file that has a `## Glossary`, byte-identical on a
second pass. That number is low only because 1 of 40 chapters has a
Glossary today — the linker's yield scales with the next full
regeneration, exactly as predicted above.

#### Step 3 — broaden criterion 8 and split its verdict [MEASURED, INCONCLUSIVE — still default OFF]

Two changes to the reviewer rubric:

- **Add flags-and-modes to the trigger examples** — `BIO_UNMAPPED`,
  `M_NOWAIT` vs `M_WAITOK`, `LK_EXCLUSIVE` vs `LK_SHARED`. Wherever the
  kernel offers two ways to do something, the reader needs to know which
  to pick and why. Today's examples are all data structures, so flags
  never register as "non-obvious mechanisms".
- **Replace the binary verdict with an enumeration**: require
  `"rationale": {"missing": [...]}` listing each unexplained mechanism.
  Enumeration resists the "found three, good enough" failure that one
  PASS/FAIL invites.

Deliberately last and unmeasured. This is a prompt edit, and this document
is largely a record of prompt edits that broke something else — so it
wants an A/B on a fixed chapter set, not a confident commit. Note step 1
already adds a narrower version of the first half: the "Undefined Terms
Detected" block tells the reviewer that where a term names a *choice*, the
gloss must say why both options exist.

**Shipped 2026-08-29, inert.** Gated behind `DAEMONDOCS_RATIONALE_ENUM`,
default OFF — the only flag in the codebase that defaults off, and the
first to change prompt text at all. Nothing changes until it is set;
`scripts/regen-runner.sh` takes it as positional arg 4 so one endpoint can
run the treatment arm against a shared queue. Revert is unsetting the env
var, not a code change. Three deviations from the spec above:

1. **The spec's JSON shape is a bug and was not implemented as written.**
   `"rationale": {"missing": [...]}` nests a list inside `criteria`, but
   `_review_passes` returns False on any non-string criteria value (and
   `_criteria_fail_count` counts it as a FAIL). That shape would fail the
   gate for *every* chapter, exhaust `max_revisions`, and ship the corpus
   UNVERIFIED. The enumeration is therefore a **top-level sibling**,
   `rationale_missing`, and `criteria.rationale` stays a string verdict.
   `tests/test_rationale_rubric.py` group 7 pins the broken shape as a
   documented negative so nobody "fixes" it back.
2. **Both halves are keyed off the one flag**, so an arm can never be
   half-applied — widening the criterion with nowhere to record findings,
   or the reverse, would each measure the wrong thing.
3. **The list is deliberately not wired into any control flow.** Not the
   approval gate (must stay criteria-driven), not `best_fails` (must stay
   comparable across arms), not `total_issues`. Its only consumer is a log
   suffix that is absent when the list is empty, so control-arm logs stay
   byte-identical. Review JSON is never persisted, so without that line the
   A/B would have nothing to measure.

Separately and *not* behind the flag: `BIO_UNMAPPED`, `LK_EXCLUSIVE` and
`LK_SHARED` were added to `_JARGON_TERMS`. That is deterministic and
mechanically checked, so it needs no A/B — but it yields *glosses*, not
rationale, so it complements step 3 rather than replacing it.

**What the A/B must measure**, on a set including the bio/buf chapter, a
locking chapter, an allocator chapter, and two architecture-prose chapters
(the last as a check that the arm does not manufacture findings where there
are no flags): rounds-to-approval and UNVERIFIED count (the main risk is
revision-round inflation — "do not stop at three" removes the reviewer's
stopping point and every entry becomes a writer instruction); criterion-8
FAIL rate at round 1, which *should* rise; `rationale_missing` count from
round 1 to last, which should trend toward 0; criterion-7 FAIL rate, since
rationale prose invites the marketing words criterion 7 rejects; and the
parse-failure rate, since a longer schema costs the single retry. None of
those decide it on their own — the target defect has no mechanical
detector, so one manual read of the bio chapter is what actually decides
keep-vs-revert.

**A/B run 2026-09-02/03 — inconclusive, flag stays OFF.** Control on
`framework` (enum=0), treatment on `framework2` (enum=1), same queue, both
arms writing the same output paths. Three of the five planned chapters
completed on both arms before this was written: ch11 (bio/buf, the
motivating defect), ch9 (locking), ch36 (mbuf/allocator). ch3 and ch18
(the architecture-prose inflation controls) were still running.

*Mechanically, the change does what it was built to do.* No faults, no
parse failures, no round inflation. Per-round `rationale_missing`:

| chapter | control rounds → final | treatment rounds → final | `rationale_missing` |
|---|---|---|---|
| ch11 bio/buf | 3 → 7/8 | 3 → 7/8 | 1 → 0 → 0 (converged) |
| ch9 locking | 3 → 6/8 | 3 → 7/8 | 2 → 0 → 1 (regressed) |
| ch36 mbuf | 3 → PASS 8/8 | 2 → PASS 8/8 | 0 → 0 (nothing enumerated) |

Criterion-8 FAIL at round 1 rose in treatment on ch11 and ch9, as
predicted. Control-arm logs carry no `rationale_missing` suffix at all, so
the byte-inertness of the OFF arm held. ch36 enumerated nothing, which is
the "does not manufacture findings" check passing on the one chapter that
had no unexplained flags.

*The manual reads do not support keeping it.* All three chapters were read
against source. Attribution is by each arm's own `<arm>-ch<N>.log`, not by
the file on disk — see the methodology warning below.

| chapter | treatment | control |
|---|---|---|
| ch11 bio/buf | `BIO_UNMAPPED` explained — **inverted** | flag absent entirely |
| ch9 locking | `ADAPTIVE_MUTEXES` explained — **correct** | `MTX_SPIN`/adaptive — **inverted** |
| ch36 mbuf | reclaim mechanism — **fabricated** | not read |

Treatment's ch11 rationale fused `BIO_UNMAPPED` with `BIO_TRANSIENT_MAPPING`
— the flag that `vfs_bio.c:4490-4492` *clears* at the moment it *sets*
`BIO_UNMAPPED`, after tearing the transient mapping down — and reversed the
causality: the flag is set because there is no KVA mapping (`bdata2bio`
takes that branch when `!buf_mapped(bp)`), and mapping is the on-demand
fallback when a consumer lacks `G_PF_ACCEPT_UNMAPPED` (`geom_io.c:481-484`),
not the default that unmapped optimizes away. Control simply dropped the
flag: zero occurrences of `BIO_UNMAPPED` or "unmapped" anywhere in its
draft.

Treatment's ch9 is the counter-example and the reason this is inconclusive
rather than a revert. It puts adaptive spinning on the *sleep* mutex,
matching `kern_mutex.c:75-76` and the three `ADAPTIVE_MUTEXES` blocks that
all live inside `__mtx_lock_sleep`. Control inverted exactly that: it
claimed a *spin* mutex "falls back to sleeping on a turnstile" under
`ADAPTIVE_MUTEXES`, when `_mtx_lock_spin_cookie` contains no turnstile or
sleepq call at all and cannot sleep by construction.

**ch36 is the finding that matters most, and it is not about step 3.**
Treatment graded PASS 8/8 with `rationale_missing=0` — the new criterion
enumerated nothing and the reviewer was fully satisfied — and the chapter
still ships a fabricated mechanism, stated three times (Quick Summary,
"Exhaustion and reclaim", "Theory connection"):

> When a `M_NOWAIT` allocation finds its UMA zone empty, it does not
> sleep; it first consults the *reclaim* path. The allocator can invoke a
> registered reclaim callback that walks socket buffers and frees mbufs
> that are safe to drop (e.g. data already acknowledged), then retries the
> allocation.

Three errors. `mb_reclaim` is registered with `uma_zone_set_maxaction`, and
`zone_maxaction` (`uma_core.c:1092-1096`) does `taskqueue_enqueue` — it is
asynchronous, so the failing allocation cannot consult the result and there
is no "then retries". The trigger is the zone *limit*, not emptiness
(`kern_mbuf.c:820-821`: "whenever any of the mbuf zones is closed to its
limit"). And it does not walk socket buffers: the entire body is
`EVENTHANDLER_INVOKE(mbuf_lowmem, VM_LOW_MBUFS)`. The parenthetical about
acknowledged data is invented outright.

**Conclusion: the variable under test is not the dominant one.** Confident,
fluent, source-shaped prose asserting false semantics about *real* symbols
appears in both arms, including in a chapter where criterion 8 was
satisfied and nothing was enumerated. Two treatment fabrications, one
treatment correction, one control fabrication, one control omission — at
n=3 there is no principled way to weight ch11's inversion against ch9's
correction. Broadening criterion 8 reliably finds unexplained mechanisms
and cannot tell whether the explanation it elicits is true, which is
exactly step 4's gap. **Step 3 should not be switched on before step 4
exists; it is a precondition, not a follow-on.** That reorders the roadmap.

**Two instrumentation defects this run exposed, both worth fixing before
any re-run:**

1. **`rationale_missing` logs only a count, never the entries.** The log
   suffix was built to give the A/B a per-round scalar, and it does — but
   every prose read then had to hunt the affected passages by grep, which
   is how the ch9 attribution went wrong on the first pass. Log the list
   contents.
2. **Both arms write the same output paths, so the tree cannot attribute
   anything.** Whichever arm finished a chapter last owns the file, and
   phase 4 (`build_navigation` / `build_chapter_index`, called with the
   *full* chapter list on every `--chapter N` run) then rewrites the nav
   and index blocks of all 40 READMEs, giving every file a uniform mtime
   unrelated to who authored its body. Reading `sys/kern/README_locking.md`
   at a moment when control's ch9 had overwritten treatment's attributed an
   inverted passage to the wrong arm; the correction came from grepping each
   arm's own chapter log for the sentence. **Attribute from
   `<arm>-ch<N>.log`, never from the file on disk** — the final draft is
   recoverable from the log's `Final answer: # <title>` marker. An A/B where
   the arms share output paths needs per-arm snapshots taken at completion,
   not after.

#### Step 4 — rationale correctness is unverifiable [OPEN, no approach yet]

Steps 1-3 raise the floor: the term gets defined, and the reviewer is
forced to account for it. **None of them can tell a correct rationale
from a plausible invented one.** The `BIO_UNMAPPED` pageout-daemon
confabulation would still ship today.

The step-3 A/B (2026-09-02/03, results above) demoted this from
"next roadmap item" to **blocking**. Three chapters read against source
produced three more confabulations of exactly this shape, in both arms:
`BIO_UNMAPPED` fused with `BIO_TRANSIENT_MAPPING` and its causality
reversed; `ADAPTIVE_MUTEXES` attached to the spin mutex with an invented
turnstile fallback; and an mbuf reclaim path described as synchronous,
limit-triggered as empty-triggered, and walking socket buffers it never
touches. The last one graded PASS 8/8 with criterion 8 satisfied and
nothing enumerated. Raising the floor on *whether* rationale is present
does not touch whether it is true, and pushing harder for rationale
produces more of it either way.

Every verifier in `generate-doc.py` is symbol-shaped: it asks whether a
name exists, whether an arity matches, whether a field is real. A claim
about *why* a design exists has no symbol to check. This is the same
class as the unverifiable ordering claims noted elsewhere — prose that
asserts causality or sequence, where grep has no purchase.

Possible directions, none prototyped:

- **Commit-message grounding.** Design rationale often lives in the
  commit that introduced the mechanism. `git log -S BIO_UNMAPPED` finds
  it; whether a writer can be made to cite it is untested.
- **Man-page and comment grounding.** `buf(9)` and the block comments in
  `vfs_bio.c` state the actual rationale. Retrieval over those, scoped to
  the mechanism under discussion, is closer to how the writer already
  uses `search_books`.
- **Targeted contradiction check.** Rather than verify a rationale, look
  for the specific shape that failed here: a causal clause ("because…",
  "in order to…") attached to a mechanism, where the named cause is a
  subsystem the mechanism has no dependency on. The pagedaemon has no
  role in `BIO_UNMAPPED`, and the call graph would show that.

Until one of these is real, treat rationale prose as the least-verified
content in the corpus. It reads authoritative, and nothing checks it.

### [DONE — shipped 2026-08-22] Disabling the writer's reasoning channel made it stop converging

Qwen3.8-27B is a reasoning model: it emits into `reasoning_content`, a
field smolagents never reads (it uses `content` only). Every thinking
token is therefore generated, paid for, and discarded. Turning it off
measured as an unambiguous win on a single call:

    thinking on : 205 completion tokens, 32.6s, 263 chars of content
    thinking off:  72 completion tokens, 10.6s, 381 chars of content

~3x faster, *more* delivered content. It was shipped for the writer on
that basis and it was wrong. The first queue run had ch3, ch4, ch5 and ch6
all hit `max_steps=80` with 3–4M input tokens each, still calling tools
sensibly at step 80 but never producing a final answer.

The controlled test — ch3 run twice concurrently, same model, same prompt,
same hardware, writer thinking the only variable:

| | thinking OFF | thinking ON |
|---|---|---|
| stages | draft → rev1 → rev2, 3 reviews | draft, 1 review (PASS first pass) |
| writer `max_steps` hits | **2** | **0** |
| repeated identical tool calls | **24x** one symbol, 22x one file | none above 2x |
| tool calls | 234 | — |

**Read the qualitative columns, not a token ratio.** An earlier version of
this entry claimed "61x" from a 19,461-vs-1,192,420 token comparison.
That number does not survive scrutiny and has been removed: the 19,461
figure came from a run whose completion was inferred from a stage list
rather than confirmed, both arms shared two endpoints concurrently, and —
decisively — every later ch3 run was contaminated by the
`_extract_fenced_function_defs` backtracking hang (documented below),
which held processes open for hours after the writer had finished. Token
totals across those runs were measuring different things.

What survives is unambiguous and does not depend on totals: with thinking
off the writer hit `max_steps` twice and called `resolve_c_definition` 24
times on a single symbol; with thinking on it hit `max_steps` zero times
and never repeated a call more than twice. It substitutes tool calls for
deliberation, exhausts the 80-step draft budget, and produces a draft the
reviewer rejects.

**The lesson is the unit of measurement** — and the meta-lesson is not to
publish a ratio until the pipeline it was measured through is known
sound. Per-call latency is the wrong unit; tokens to a *finished chapter*
is the right one. Any future "make the model cheaper per call" change
needs a two-arm test, on a quiet machine, with both arms confirmed to
have actually completed.

Writer and reviewer reasoning are now separately env-gated
(`DAEMONDOCS_WRITER_THINKING`, `DAEMONDOCS_REVIEWER_THINKING`, both
default on) so the experiment is repeatable and so the two endpoints can
run opposite *reviewer* arms against the shared queue — the reviewer case
is still unmeasured, and it is a genuinely open question, because the
ch11 runaway that forced reviewer `max_steps` 15→5 was 121K tokens of
thinking with no parsed code.

**Mechanism trap worth remembering:** `chat_template_kwargs` must be
passed inside `extra_body={...}`. The openai SDK validates keyword
arguments against `Completions.create()`'s signature and raises
`TypeError` on unknown names, so the top-level form fails *every call
before it reaches the wire*. It still appears in `model.kwargs`, so
inspecting that proves nothing. This cost 4 chapters: each failed in ~60s
while the script exited 0, so the queue runner read it as success and
kept popping. Two guards were added — `generate-doc.py` now exits
non-zero when it produced no chapter, and `runner.sh` aborts after 2
consecutive failures rather than draining the queue.

### [DONE — shipped 2026-08-23] Nothing capped per-step generation; three steps ate 87% of a chapter's wall clock

ch3 spent 4h47m on 16 steps of a draft. The distribution, not the total,
is the story:

    step  5   3782s   (~26k tokens)
    step 11   5942s   (~40k tokens)
    step 14   3162s   (~21k tokens)
    -------------------------------------------
    3 steps = 12,886s of 14,863s = 87% of wall clock

The other 13 steps averaged 152s. Two candidate causes — prompt-cache
misses (a config fix) versus runaway generation (a different fix) — and
llama-server's `/metrics` separated them in minutes:

    during a slow step:  prefill 0 tokens, 0 seconds
                         decode  306 tokens in 45s = 6.8 tok/s
    cumulative:          prefill 113 tok/s (15% of time)
                         decode  6.7 tok/s (85% of time)
                         prompt cache 96% hit rate

Pure generation. Prefill was already fully cached, so `--cache-reuse` and
friends would have bought nothing.

**Nothing capped it at any layer**: `generate-doc.py` never set
`max_tokens`, smolagents has no default, and llama-server's `n_predict`
was unset. One step could therefore generate until it exhausted the
131072-token context — 5.4 hours at this hardware's 6.8 tok/s.

This is the writer-side twin of the ch11 reviewer runaway (121K tokens of
thinking, no parsed code). That one was bounded indirectly by dropping
reviewer `max_steps` 15→5. `max_steps` cannot help here: the problem is
one step, not many.

Fix: `WRITER_MAX_TOKENS` (env `DAEMONDOCS_WRITER_MAX_TOKENS`, default
16384, `0` restores unbounded). Sized from measured chapters, not
estimates: `README_internals.md` 22076 B (~6.1k tokens), the ch2 loader
chapter 24006 B (~6.7k), the ch3 draft 20906 B (~5.8k). The cap sits at
~2.4x the largest real output. It does not need to sit close to that
ceiling to work — the runaways were 21k-40k tokens, 3-6x any real
chapter — and the failure mode of a too-tight cap is nasty: every draft
truncates, best-draft tracking ships the least-bad partial, and nothing
looks broken. Prefer headroom.

Verified end-to-end against a live endpoint (`completion_tokens: 64`,
`finish_reason: length` with a test cap of 64), because a kwarg that
merely appears in `model.kwargs` is not proof it reaches the wire — see
the `extra_body` trap in the reasoning entry.

**Truncation is reported, not silent.** `_warn_on_token_truncation`
inspects `finish_reason` on every step after each agent run and prints a
warning naming the cap and the env var to raise. Without it, hitting the
cap is invisible — the server says `length`, smolagents drops it, and
the pipeline just sees a short response. Verified both directions
against the live endpoint: `length` at a 32-token cap produces the
warning, `stop` at 2048 stays silent.

**Tradeoff, stated plainly:** a truncated step can produce a malformed
action and cost a retry. That is still far cheaper than 99 minutes, and
`_looks_like_stub` plus best-draft tracking already absorb bad output.
Note also this is one chapter's data — ch1 and ch2 averaged 96-126s/step
with no outliers at all, so the cap is insurance against a pathological
case rather than a fix for a universal one.

**Question this answers:** "should we split chapters into sub-sections to
spend less time per chapter?" No. Splitting reduces steps per chapter and
does nothing about a single step generating 40k tokens; it would add a
draft/review/fact-check cycle per split while leaving the cost driver
untouched. Measure the distribution before restructuring.

### [DONE — shipped 2026-08-23] Catastrophic regex backtracking wedged a finished chapter for 6.8 hours

`_FENCED_FUNC_DEF_RE` in `_extract_fenced_function_defs` used
`(?:[A-Za-z_]\w*\s*\**\s+)+` for the return-type token run. That group is
ambiguous with the `\*?\s*` that follows it — both can absorb the pointer
stars and the whitespace — so whenever the trailing `\{` failed to match,
the engine re-split the same token run exponentially many ways.

The observed failure, on ch3:

    python:   state R, 99% CPU, 414 min CPU time, 1.1 GB RSS
    endpoint: requests_processing 0        <- the LLM was idle
    log:      silent for 6.8 hours

The writer had **already finished the chapter** — a complete draft with
real symbols and a full See Also section, at step 31. The pipeline then
entered fact-checking and never came back. Every other extractor handled
the same 20KB draft in under 0.1s.

Two things made this expensive out of proportion to the bug:

- **It looked like slowness, not a hang.** The process was at 99% CPU, so
  every check said "working". It took bisecting extractors by hand to find
  the real culprit, and in the meantime the token counts it corrupted led
  to a wrong published conclusion (see the reasoning-channel entry above).
- **The runner's watchdog only kills.** A log-mtime watchdog eventually
  SIGTERMs the process, which tells you *that* something hung and nothing
  about *where*.

Fixes:

1. **Atomic group + bound**: `(?>(?:[A-Za-z_]\w*\s*\**\s+){1,8})`. The
   atomic group commits to each token and never re-divides it, removing
   the exponential blowup; the `{1,8}` bound caps the worst case whatever
   a future edit does to the surrounding pattern. 6.8 hours → 0.000s on
   the exact input that hung, with all seven definition shapes still
   matched and both negative cases still rejected.
2. **A general hang detector** (`install_hang_detector`, `heartbeat`,
   `beat` in the Configuration banner). A daemon thread dumps every
   thread's Python stack when the main thread stops checking in
   (`DAEMONDOCS_HANG_DUMP_SEC`, default 1800s, 0 disables). It uses
   `sys._current_frames()` plus `faulthandler`, so it still prints when
   the main thread is stuck inside a C-level regex loop — a pure-Python
   timer cannot interrupt those. It is **advisory**: it never kills
   anything, it just makes the failure legible in the chapter log.
   Killing stays the runner's job, where a human set the policy.
   Output is explicitly flushed, because chapter logs are redirected to
   files and a block-buffered dump would never reach disk before the
   process was killed — the exact failure it exists to prevent.
3. **A scan budget** in `_extract_fenced_function_defs` so the per-block
   loop cannot compound across a document, with a warning in the log.
   Note this cannot interrupt a single pathological match; the atomic
   group is the actual fix, this is the seatbelt.

**The detector's own false positive, worth recording.** Shipped at a
300s threshold, it fired within minutes on a perfectly healthy draft.
The dump showed the main thread blocked in `openai/_streaming.py` — the
writer simply waiting on the model. Two mistakes: 300s was below
legitimate LLM latency (a single real step had already been measured at
1164s earlier the same day, and I failed to apply my own number), and
the phase label read `'startup'` because agent stages never called
`beat()`. A detector that cries wolf is worse than none, because the
next dump gets ignored.

That led to the better design: **ask the endpoint instead of guessing.**
`_endpoint_is_decoding()` samples llama-server's `/metrics` twice and
suppresses the dump only when the model demonstrably produced tokens in
between, so wall-clock time stopped being the sole signal. Use
`n_decode_total`, NOT `tokens_predicted_total` — the latter only rolls
up when a request completes, so it sits frozen for the whole generation
(measured: frozen at 191310 while `n_decode_total` climbed 135 per 20s).
The probe fails safe: no `/metrics`, unreachable host, or flat counter
all read as "not decoding" so the detector still fires — the original
bug was a local CPU spin with the endpoint *idle*, which is exactly a
flat counter. `install_hang_detector()` prints at startup which mode is
active, because a silent degradation to pure-timeout is how you end up
trusting a check that is not really checking. llama-server needs
`--metrics` for the good mode.

Regression tests in `tests/test_regex_hang.py`, including the real 20KB
draft as a committed fixture (`tests/testdata/ch3-fenced-hang.md`) — a
repro that lives in `/tmp` is not a regression test — plus a guard that
the threshold stays above observed legitimate latency and that the probe
reads the right counter.

**Generalizable lesson:** every user-content-driven regex in the
fact-checker is a potential wedge. The ones with nested quantifiers under
a `+` or `*` are the candidates worth auditing; `re` has no backtracking
limit and no timeout, so a bad pattern is an unbounded hang, not a slow
match.

### [DONE — shipped 2026-08-22] Path fact-check never looked at directories; ch1 shipped 9 bad paths as "clean"

`_extract_file_paths` required a trailing file extension
(`\.(?:c|h|s|rs|md|9|4|5|7|8)`), so directory claims and extensionless
files were never extracted and so never verified. This matters most
exactly where it hurts most: ch1 (Source Tree — Layout and Conventions)
is almost entirely directory prose.

Measured on the K4 ch1 (`README_internals.md`, 2026-04-30):

| | count |
|---|---|
| backticked paths in the chapter | 106 |
| genuinely absent from the tree | 9 |
| **extracted by the pipeline** | **3** |
| **flagged by the pipeline** | **0** |

Among the misses was `gnu/` — a subtree FreeBSD *retired upstream*
(`134a4c78d070 "Retire the GNU subtree"`). The chapter documented a
directory that no longer exists, which is the same stale-training-data
failure mode as fabricated struct fields, and the fact-checker reported
clean. A verifier that silently skips a whole claim class is worse than
no verifier: it produces a false PASS.

Extraction now also takes directory-shaped tokens and known extensionless
filenames (`Makefile`, `COPYING`, `Kyuafile`, …). The hard part was not
extraction but **staying quiet**: the first attempt flagged 44 paths on
ch1, mostly correct prose. Verification therefore exempts absolute paths
(installed-system locations, legitimately absent from a checkout),
`src/`-relative paths, kernel-relative paths (`sys/proc.h` is really
`sys/sys/proc.h`; bare `kern`/`vm`/`amd64` resolve under `sys/`), and the
`machine/` per-arch include alias.

Two attempts were reverted, both caught by testing against real chapters
rather than synthetic strings:

- **Basename-only glob correction.** Produced `sys/conf/config →
  sys/contrib/openzfs/config`. A wrong correction is worse than none —
  the writer trusts it and rewrites correct-ish prose into an unrelated
  file. Corrections must now share the claimed parent directory.
- **Trailing slash as a "top-level directory" signal.** Intended to
  separate the retired top-level `gnu/` from `sys/gnu`. Chapters write
  `kern/`, `vm/`, `amd64/` with slashes too, so it took ch1 from 9 flags
  to 29. Reverted, with the miss documented rather than papered over.

Final state across all 28 existing chapters: **397 paths extracted, 13
flagged, all 13 verified genuine, zero false positives.** Regression
tests in `tests/test_path_factcheck.py` pin both the catches and the
exemptions, including the deliberate `gnu` miss.

**Still open in principle — ordering claims:** every verifier in the
pipeline is *symbol-shaped* (does this name exist?). None can check a
claim about *sequence*, where each named thing is real and only the
order between them is wrong. A sequence verifier would extract ordered
claims (`A → B`, "first X then Y", Mermaid participant chains) and test
them against the call graph via `trace_path`, reporting "no path from A
to B" as *suspect* rather than *hallucinated* — the conservative framing
the sysctl checker uses.

**It is NOT yet built, deliberately, because the motivating example
turned out to be wrong.** The `boot1 (GPT)` stage in ch2 was scored as a
phantom for several runs. Checked against the tree on 2026-08-24:

    stand/efi/boot1/            exists, and is BUILT
                                (stand/efi/Makefile: SUBDIR.yes+= boot1)
    proto.c:129 load_loader()   calls mod->load(PATH_LOADER_EFI, ...)
    common/paths.h:34           PATH_LOADER_EFI = "/boot/loader.efi"

So `boot1 → loader.efi` is a real, traversable code path and the marker
in `check_ch2.sh` was a false positive (now downgraded to informational).
The residual criticism is far narrower and not mechanically checkable: a
typical UEFI install has the firmware load `loader.efi` directly from the
ESP, with boot1 used when booting from a UFS/ZFS partition without an ESP
entry — so presenting boot1 as *the* path rather than *one* path is
oversimplification, not hallucination.

Build this when a genuine ordering hallucination appears in a run, not
before. A verifier written against a hypothetical is how you get a check
nobody trusts — see the 300s hang-detector threshold in the entry below,
which fired on healthy work because it was calibrated by intuition rather
than measurement.

### [DONE — shipped 2026-08-20] Sysctl OID paths were unverifiable by grep; added optional graph-backed fact-check

Sysctl OIDs (`net.inet.tcp.*`, `vm.pmap.pde.mappings`, `kern.ipc.maxsockbuf`)
are a favorite writer hallucination — the model emits a plausible-sounding
tunable and every existing fact-check passes it, because **grep cannot
verify a sysctl OID at all**. The dotted path exists nowhere in the source
as a literal string; it is assembled at compile time from a chain of
`SYSCTL_*` macros:

```c
SYSCTL_NODE(_vm,       OID_AUTO, pmap, ...)              /* vm.pmap        */
SYSCTL_NODE(_vm_pmap,  OID_AUTO, pde,  ...)              /* vm.pmap.pde    */
SYSCTL_COUNTER_U64(_vm_pmap_pde, OID_AUTO, mappings, ..) /* .pde.mappings  */
```

`grep vm.pmap.pde.mappings` returns 0 for this REAL sysctl exactly as it
does for a fabricated one. To verify an OID you must parse the macro tree
and walk the `_vm_pmap_pde` parent chain — which is precisely what the
`codebase-memory-mcp` indexer does, storing each reconstructed OID as a
`Sysctl` graph node keyed by its full dotted path.

**What shipped:** a new fact-check verifier pair, `_extract_sysctls` +
`_verify_sysctls_via_graph`, that pulls canonical-root OID tokens
(`kern hw vm net dev vfs debug security machdep user compat kstat
p1003_1b`, ≥2 dotted segments, backticked) from the draft and verifies
each with an anchored `search_graph --name-pattern '^oid$' --label Sysctl`
query against the graph. Real OIDs resolve to their registration line;
fabricated OIDs return zero hits. Wired into `fact_check_draft` as
`sysctls_not_found` and into `_build_fact_check_prompt`.

**Why it's OPTIONAL, not a dependency:** this is the only verifier with an
external backend. `_sysctl_graph_available()` probes for the binary and a
ready index once per process; when either is missing it prints a single
warning and `_verify_sysctls_via_graph` returns `[]` (nothing flagged). A
host without codebase-memory-mcp runs the full pipeline unchanged, minus
sysctl checking. This was a hard requirement — the script's execution host
has the tree, but a contributor's checkout may not have the graph
indexed.

**The false-negative caveat (why "suspect", not "hallucinated"):** ~1/3 of
`Sysctl` nodes are `resolved:false` — the indexer built the leaf but could
not resolve the parent OID chain (parent name came from a macro argument,
a runtime string, or `LINUXKPI_PARAM_NAME(...)`-style expansion). Those
nodes carry a `<unresolved>.leaf` path and never match a real dotted query,
so a genuine OID among them would be reported "not found". That is the SAFE
failure direction for a hallucination gate (over-flag, never under-flag),
and the fact-fix prompt reflects it: it tells the writer the OID is
*suspect — confirm against the registering `SYSCTL_*` macro or `sysctl(8)`*,
not that it is definitely invented. Tests in `tests/test_sysctl_factcheck.py`
(extractor tests always run; graph-verify tests auto-skip when the backend
is absent).

**Backstory:** the K8 regen (2026-05-02, see
`project_k8_regen_failed` memory) got *worse* on hallucination after a
quant upgrade; the demand was to fix hallucination in code before any
re-run. The MCP was evaluated and initially rejected for fact-check —
early builds stored sysctls only as `Macro` nodes with no OID path, so the
graph could not distinguish a real sysctl from a fake one any better than
grep. After the codebase-memory-mcp maintainer added OID-tree
reconstruction (`Sysctl` nodes with a resolved `path`), a re-index proved
the discrimination worked (real accepted, fabricated flagged), and this
verifier was built on it.

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
`tests/test_struct_factcheck.py`.

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
and refuses to render. Test harness: `tests/test_mermaid_sanitizer.py`
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

Test harness: `tests/test_link_sanitizer.py` (18 sub-checks: the
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

   **Amended 2026-09-01: fact-fix now retries once too.** Rolling back a
   fact-fix is not the same trade as rolling back a revision. A
   rolled-back revision costs polish; a rolled-back fact-fix ships
   claims the fact-checker has already proven false. ch21 (pf, run
   finished 2026-08-27T14:58Z) is the case: reviewer PASS 8/8, then
   fact-check found DTrace probes in no `SDT_PROBE_DEFINE*` macro, a
   hallucinated `pf_state_cmp` sub-struct, and a suspect `net.pfil`
   sysctl; fact-fix stubbed, `keeping pre-fact-fix draft` fired, and all
   of it shipped under the UNVERIFIED banner. The retry reuses the
   fact-check prompt (so the flagged symbols stay in front of the
   writer) plus the same explicit `final_answer()` reminder the
   initial-draft retry uses. Bounded at exactly one extra call, and if
   the retry also stubs the old path runs unchanged — pre-fact-fix draft
   kept, `fact_fix_failed` set, banner emitted. Revision stubs are
   deliberately left alone. Test: `tests/test_factfix_stub_retry.py`
   (verified to fail on the pre-change code, 8 named failures).
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

**SUPERSEDED 2026-05-02 — the mandatory Comparison section was
removed entirely.** Even with the comparison-quality criterion in
place, the K8 overnight regen left ~22 chapters UNVERIFIED, and the
dominant failure mode was still cross-OS hallucination: the writer
either emitted tautologies that scored FAIL or fabricated Linux/
NetBSD/OpenBSD internals that the deterministic FreeBSD-source fact-
checker couldn't grade either way (`_strip_comparison_section`
exempts the region by design). User considered downloading Linux/
NetBSD/OpenBSD sources but rejected it: comparison claims are
*behavioral*, not symbolic — even with sources on disk, deterministic
grep can't verify "Linux's vm_area_struct fragments more aggressively
than FreeBSD's vm_map under fork." Symbol-existence checks would
catch a small fraction of what comparison claims assert.

Removing the section entirely eliminated this class of hallucination
at the source: no prompt for cross-OS commentary, no review
criterion grading it, no fact-checker-shaped exemption. The default
section list dropped from 8 to 7 sections; the rubric from 9 to 8
criteria. The training-data contamination warning to the writer
("Linux/NetBSD/OpenBSD field names slip in even when you don't
mention those OSes by name") stays — that's about FreeBSD-symbol
verification, not comparison-section content. `_strip_comparison_
section` is kept as a no-op safety net for legacy on-disk drafts
during touch-up fact-check passes.

Chapters that legitimately benefit from a small in-line analogy can
include it within Architecture or Advanced Notes (no separately-
graded section, no auto-fill pressure).

### [PARTIALLY DONE — shipped 2026-05-02] Struct-snippet faithfulness — code blocks can disagree with prose, nothing checks the layout

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

**Status (2026-05-02): partially shipped.** Triggered by ch2
(stand/efi/loader/README.md, K8 regen) which shipped UNVERIFIED with
fabricated `bi_efi_memmap`, `bi_efi_memmap_size`, `bi_modlist` fields
on `struct bootinfo` and a fabricated `bi_construct()` function body.
What's now caught (`generate-doc.py` 2026-05-02):

- `_extract_struct_field_claims` + `_verify_struct_field_claims`:
  member-access expressions (`var->field`, `var.field`) where `var`
  is bound to a struct by an in-block `struct NAME *var` declaration,
  AND prose `STRUCTNAME->FIELD` paraphrases where `STRUCTNAME` is the
  type being used as a variable. Wired into `fact_check_draft` as
  `struct_field_refs_bogus`. ch2's three bogus fields all surface.
- `_extract_fenced_function_defs`: function definitions inside fenced
  ```c blocks (a return-type chunk, NAME(...), `{` body opener at line
  start). Unioned into `_extract_function_names` so existing
  `_verify_functions` flags fabricated bodies. Catches `bi_construct`.
- `_extract_struct_names` extended: also matches ``\`NAME\` structure``
  in prose. Catches `bi_module` (chapter referenced "a `bi_module`
  structure" with no `struct` keyword).
- `_FILE_EXT_DENYLIST`: rejects `bootinfo.c`-shaped path tokens that
  would otherwise be misread as `var.field`. Tested.

What's still open:

- **Original mbuf failure mode — DONE-with-caveats (2026-05-02).** Option (2)
  from the post-mortem is now implemented. `_verify_struct_bodies` returns
  a second list `struct_bodies_abridged`: any fenced ` ```c struct NAME { ... } ```
  block whose claimed-field set has *zero overlap* with the real top-level
  fields (and whose real definition has ≥4 fields, and which is not marked
  abridged) is flagged. Abridgement markers recognized: `...`, `\u2026`,
  and comments containing `elided`/`omitted`/`truncated`/`simplified`/
  `abridged`/`abbreviated`/`for brevity`/`other fields`/`additional fields`/
  `more fields`/`rest of`. Wired into `fact_check_draft` and the fact-fix
  prompt (`_build_fact_check_prompt`), so the writer is told specifically:
  "your body has zero overlap with the real definition, quote the real
  field names verbatim or use `/* ... */` to elide." Two parser bugs were
  fixed as prerequisites: `_real_struct_fields` now ranks candidates
  (canonical kernel-header dirs first) AND picks max-fields across the
  top 32 candidates — previously a 2-field test stub would beat the real
  28-field `sys/sys/mbuf.h`; and `_parse_struct_fields` now recurses into
  anonymous `union { ... };` / `struct { ... };` bodies (mbuf has 3+
  levels of nested anonymous unions, which the old regex flattened
  incorrectly). What's still open: (a) bodies with *some* real fields
  (e.g. 2 of 28) but mostly fabricated names — the threshold is currently
  "zero overlap" because choosing K is hard; raising K creates false
  positives on legitimate elided drafts. (b) ~~The K&R-style brace-on-
  next-line definition (`struct foo\n{`) is found by `_real_struct_fields`
  but not by the reviewer-side `_verify_structs`.~~ **Fixed same day**
  (2026-05-02): the shape-grep and Python pattern in `_verify_structs`
  were extended to accept either `^struct NAME *\{` (same-line brace)
  or `^struct NAME *$` (K&R brace on next line). Discovered when ch2
  test run failed accuracy on the real `struct preloaded_file` (K&R
  brace in `stand/common/bootstrap.h:230`). Forward declarations and
  pointer uses are still rejected. Regression test added to
  `tests/test_hallucination_factcheck.py` (Test 11b).
- **Comparison-section claims.** ~~`_strip_comparison_section`
  deliberately exempts that section. ch2's NetBSD/Linux/macOS
  comparison rows still ship without verification.~~ **Resolved
  2026-05-02 by removing the mandatory Comparison section
  entirely.** See the SUPERSEDED note at the top of this file for
  rationale.
- **Verifier scope `sys/` only — DONE-with-caveats (2026-05-02).** Per-
  chapter `extra_search_dirs:` knob added. ch2 (Boot Process) sets
  `extra_search_dirs: ["stand"]` so `preloaded_file`, `file_metadata`,
  `elf64_exec`, `EFI_MEMORY_DESCRIPTOR` etc. now verify against
  `~/freebsd-src/stand/`. `_resolve_search_roots(src_root, extra_dirs)`
  always includes `<src>/sys` and appends each existing extra; the cache
  key embeds a sorted-tuple suffix so widening one chapter's roots does
  NOT poison the sys-only cache for others. All four major verifiers
  (`_verify_structs`, `_verify_functions`, `_real_struct_fields`,
  `_verify_with_cache`) thread the kwarg through. `fact_check_draft` and
  `build_review_prompt` both read `chapter.get("extra_search_dirs")`.
  What's still open: only ch2 currently opts in; other chapters that
  reach into `lib/`, `usr.bin/`, `usr.sbin/` will still false-flag
  userland-only symbols until they're given the knob.
- **Reviewer didn't penalize hallucination.** ch2 scored 8/9 from the
  reviewer, then the fact-fix loop landed with the hallucinations
  intact. The new verifier output now feeds the fact-fix prompt with
  specific bogus names — the writer should patch them out — but the
  reviewer's Accuracy criterion still doesn't gate on prose-vs-source
  field-name consistency. Separate work.

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
