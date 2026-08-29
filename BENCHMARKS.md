# DaemonDocs — Endpoint Benchmarks

Per-endpoint generation-speed measurements from full-corpus runs.
Each run uses the same `generate-doc.py` pipeline (writer + reviewer
+ fact-check, max 3 revisions per chapter, default sampling) over
the same `chapters.yaml`, distributed across multiple llama-server
endpoints via the queue runner in `/tmp/regen-queue/runner.sh`.

The goal of this file is to compare **model speed** and **hardware
backend** across runs. New runs with different models append a new
section; the schema below stays stable so direct comparison is easy.

## Schema

Each run records:

- **Run ID**: `runN-YYYY-MM-DD` (chronological).
- **Pipeline state**: short note about what was new in `generate-doc.py`
  for that run (rubric criteria count, fact-check stages, etc.) so
  speed differences from pipeline changes don't get conflated with
  model speed.
- **Per-endpoint table** with: hardware, OS, llama.cpp build, model,
  llama-server CLI, chapters generated, per-chapter min/avg/max/total.
- **Wall-clock**: time from first-runner-launch to last-runner-exit.
- **Notes**: observed failure modes, queue-strategy quirks, anything
  that affects interpretation of the speed numbers.

Per-chapter durations are parsed from
`/tmp/regen-queue/{label}.log` "starting chapter N" → "chapter N
finished" timestamp deltas. Chapters with rc≠0 are excluded from the
average but counted in the failure column.

---

## run1 — 2026-04-30 → 2026-05-01

**Pipeline state at time of run:**

- 9-criteria reviewer rubric (rationale + comparison_quality
  added 2026-04-30).
- Per-chapter `sections:` opt-out for Comparison (ch4, ch25).
- Up: breadcrumb in nav sidebar (`_build_ancestor_chain`).
- See Also relpath fix (`os.path.relpath`).
- 25 chapters in `chapters.yaml` (ch25 = netgraph, first generation).
- **No** LLM HTTP timeout, **no** struct-body fact-check (both
  identified as gaps during this run; timeout shipped 2026-05-01).

**Model used (all three endpoints, identical):**

- **GGUF**: `unsloth/Qwen3.6-35B-A3B-GGUF`,
  `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf`
  (snapshot `a483e9e6cbd595906af30beda3187c2663a1118c`)
- **llama.cpp build**: `b8985-27aef3dd9`
- **Context**: 131072
- **Sampling**: `temperature=0.6 top_p=0.95 top_k=20 min_p=0.0`,
  `--flash-attn on`, `--batch-size 2048 --ubatch-size 512`,
  `--parallel 1`

### Endpoints

| Endpoint | Hardware | OS | Backend | Driver |
|---|---|---|---|---|
| **fw1** | AMD Ryzen AI Max+ 395 (32 cores) + Radeon 8060S iGPU, 128 GB | FreeBSD 16.0-CURRENT (main-n285413, 2026-04-25) | Vulkan (`--device Vulkan0`) | Mesa RADV 24.1.7 |
| **fw2** | AMD Ryzen AI Max+ 395 (32 cores) + Radeon 8060S iGPU, 122 GB | Ubuntu 24.04 / Linux 6.17.0-22-generic | Vulkan (`--device Vulkan0`) — also detected ROCm | Mesa RADV 25.2.8 |
| **mac** | Apple M3 Pro (12 cores) + integrated GPU, 36 GB unified | macOS 26.4.1 (25E253) | Metal (`--device MTL0`) | Metal 4 |

Note: fw1 and fw2 are physically the same machine type (AMD Ryzen
AI Max+ 395 / Radeon 8060S) — the only differences are OS (FreeBSD
vs Linux) and Mesa version (24.1.7 vs 25.2.8). They make a clean
A/B test of the FreeBSD-vs-Linux Vulkan stack on identical silicon.

### Per-endpoint speed

Computed from runner-log timestamp deltas (start → finish per chapter).

| Endpoint | Successful | Failed | Total min | Avg min/ch | Min | Max |
|---|---|---|---|---|---|---|
| fw1 | 10 | 0 | 631 | **63.1** | 17.9 | 130.8 |
| fw2 | 10 | 0 | 607 | **60.7** | 19.4 | 120.1 |
| mac | 4 | 1 | 556 | **139.1** | 110.5 | 166.9 |
| **all** | **24** | **1** | **1794** | 74.8 | 17.9 | 166.9 |

**Wall-clock**: launched 2026-04-30 20:08:22 CEST, last runner
exited 2026-05-01 06:39:33 CEST. Total wall-clock
**10h31m11s** for 24 chapters across 3 endpoints.

### Per-chapter detail

Output from the parsing script (see "Reproduction" below). Chapter
ID is the `chapters.yaml` index.

```
=== fw1 ===
  ch  3    44.6 min   rc=0      VFS
  ch 20   130.8 min   rc=0      NIC drivers
  ch  6    65.0 min   rc=0
  ch 12    72.5 min   rc=0
  ch  1    17.9 min   rc=0      Source Tree (mostly prose)
  ch 25    54.4 min   rc=0      netgraph (first generation)
  ch 11   100.8 min   rc=0
  ch 16    56.0 min   rc=0      pf
  ch 19    36.9 min   rc=0
  ch 24    52.4 min   rc=0      DTrace

=== fw2 ===
  ch 15    92.3 min   rc=0      Network Stack
  ch  9    60.0 min   rc=0      GEOM
  ch  5    36.2 min   rc=0
  ch 22    94.4 min   rc=0      Capsicum
  ch 14   120.1 min   rc=0
  ch 10    64.9 min   rc=0      CAM
  ch  4    39.9 min   rc=0      Build System
  ch 18    37.8 min   rc=0
  ch 21    19.4 min   rc=0
  ch 23    42.5 min   rc=0      bhyve

=== mac ===
  ch 17   110.5 min   rc=0      ipfw + dummynet
  ch 13   112.2 min   rc=0      ZFS
  ch  8    41.5 min   rc=1      *FAIL* — JSONDecodeError on inner quotes
  ch  7   166.8 min   rc=0      Locking primitives
  ch  2   166.9 min   rc=0      Boot process
```

### Headline findings

1. **fw1 ≈ fw2** within noise (63.1 vs 60.7 min/ch). Same hardware,
   same Vulkan backend, only the OS differs — and the difference
   is statistically irrelevant. **FreeBSD's Vulkan stack does not
   pay a visible penalty vs Linux for this workload.**
2. **mac is ~2.2× slower per chapter, uniformly.** Its fastest
   chapter (110 min) is nearly twice the median for fw1/fw2. This
   is not a tail-latency issue — every chapter took longer.
3. The slowdown is **purely hardware/backend** — same model, same
   llama.cpp build, same context, same sampling. Apple M3 Pro
   integrated GPU + Metal vs Radeon 8060S iGPU + Vulkan, on this
   workload, the AMD setup is roughly 2× faster.
4. **Largest-first queueing routed long chapters to the slow
   endpoint** by accident — mac happened to be free when ch17,
   ch13, ch7, ch2 came up (all 110+ min on mac). Recorded as an
   open improvement in `FUTURE_IMPROVEMENTS.md` ("Queueing
   strategy assumes endpoints are equal speed").

### Failure modes observed during this run

- **ch8 crashed with `JSONDecodeError`** when the reviewer emitted
  a string value containing unescaped inner double quotes
  (`"discussion of "background processing""`). Fixed 2026-05-01
  by wrapping the brace-matched-substring `json.loads` call in
  try/except.
- **Stalled HTTP call** observed on the first ch8 re-run: writer
  mid-step, llama-server at 0% CPU, log frozen 6+ min, no recovery.
  Fixed 2026-05-01 by adding a 600s read timeout to the OpenAI
  client (`MODEL_CONFIG["timeout"]`,
  `client_kwargs={"timeout": ...}` on `OpenAIServerModel`).
- **21 of 25 chapters wrote with the UNVERIFIED DRAFT banner** —
  the new 9-criteria rubric catches more issues than the old 7,
  and the writer's revision loop doesn't always converge within
  3 rounds. Tracked under "Struct field names and function-call
  mechanics not grounded in source" (top-priority OPEN entry).

### Reproduction

To re-derive the per-chapter table from any future run:

```bash
ssh framework 'cat /tmp/regen-queue/{fw1,fw2,mac}.log' > /tmp/all-runner-logs.txt
python3 - <<'PY'
import re
from datetime import datetime
from collections import defaultdict
pat = re.compile(r'^\[(\w+) (\w+ \w+\s+\d+ \d+:\d+:\d+) \w+ (\d+)\] (.+)$')
events = defaultdict(list)
for ln in open('/tmp/all-runner-logs.txt'):
    m = pat.match(ln.rstrip())
    if not m: continue
    label, dt_str, year, msg = m.groups()
    dt = datetime.strptime(f"{dt_str} {year}", "%a %b %d %H:%M:%S %Y")
    events[label].append((dt, msg))
for label in ('fw1','fw2','mac'):
    starts = {}
    rows = []
    for dt, msg in events[label]:
        m = re.match(r'starting chapter (\d+)', msg)
        if m: starts[m.group(1)] = dt; continue
        m = re.match(r'chapter (\d+) finished rc=(\d+)', msg)
        if m and m.group(1) in starts:
            ch, rc = m.group(1), m.group(2)
            dur = (dt - starts[ch]).total_seconds() / 60
            rows.append((ch, dur, rc))
    print(f"=== {label} ===")
    for ch, dur, rc in rows:
        flag = "" if rc == "0" else "  *FAIL*"
        print(f"  ch{ch:>3}  {dur:6.1f} min   rc={rc}{flag}")
    rc0 = [d for _, d, rc in rows if rc == "0"]
    if rc0:
        print(f"  -- {len(rc0)} ok, total {sum(rc0):.0f} min, "
              f"avg {sum(rc0)/len(rc0):.1f} min/ch, "
              f"min {min(rc0):.1f}, max {max(rc0):.1f}")
PY
```

The queue file used:

```
3 15 17 20 9 13 5 6 22 8 12 7 14 1 25 11 10 2 4 16 18 19 21 23 24
```

(largest-first ordering — see "Queueing strategy" entry in
`FUTURE_IMPROVEMENTS.md` for why this is suboptimal when one
endpoint is 2× slower than the others.)

---

## run2 — 2026-05-02 (partial, interrupted)

**Pipeline state at time of run:**

- Still 9-criteria reviewer rubric (this run predates the 2026-05-02
  Comparison-section removal — see run3 once that run lands).
- LLM HTTP read timeout (600s) shipped 2026-05-01 (gap identified
  in run1).
- Struct-body fact-check added 2026-05-01: post-revision pass
  that greps `freebsd-src` for the structs cited in
  `## Key Data Structures` and asks the writer to fix any whose
  field list disagrees with `sys/`.
- Extended `_FACT_CHECK_IGNORE` denylist for cross-OS struct
  names (`vm_area_struct`, `task_struct`, etc.) so passing
  references in Comparison sections don't burn fact-fix steps.
- Same 25 chapters in `chapters.yaml` as run1.
- Topology change: dropped the `mac` endpoint; both runners now
  drive the AMD Vulkan endpoints (fw, fw2). The script runs from
  `bigone` and talks to remote llama-servers via OpenAI API —
  see CLAUDE.md / CODE_MAP.md "Execution topology."

**This run was interrupted at 13/22 attempted chapters** to apply
the new Comparison-section removal (run3 picks up the rest with
the new pipeline). Numbers below are valid for the 13 chapters
that completed; treat the run as a snapshot, not a corpus.

**Model used (both endpoints, identical):**

- **GGUF**: `unsloth/Qwen3.6-35B-A3B-GGUF`,
  `Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf` (Q8 vs run1's Q4)
- **llama.cpp build**: `b8985-27aef3dd9` (unchanged from run1)
- **Context**: 131072
- **Sampling**: same as run1 (`temperature=0.6 top_p=0.95 top_k=20
  min_p=0.0`, flash-attn on, batch 2048 / ubatch 512, parallel 1)

### Endpoints

| Endpoint | Hardware | OS | Backend | Driver |
|---|---|---|---|---|
| **fw** (FreeBSD host) | AMD Ryzen AI Max+ 395 (32 cores) + Radeon 8060S iGPU, 128 GB | FreeBSD 16.0-CURRENT | Vulkan (`--device Vulkan0`) | Mesa RADV 24.1.7 |
| **fw2** (Linux host) | AMD Ryzen AI Max+ 395 (32 cores) + Radeon 8060S iGPU, 122 GB | Ubuntu 24.04 / Linux 6.17.0-22-generic | Vulkan (`--device Vulkan0`) | Mesa RADV 25.2.8 |

Same physical hardware as run1's fw1/fw2 — relabeled `fw`/`fw2` to
match the current endpoint names (`framework`, `framework2`).

### Per-endpoint speed

| Endpoint | Successful | Failed | Total min | Avg min/ch | Min | Max |
|---|---|---|---|---|---|---|
| fw  |  7 | 0 |  592 | **84.5** |  69.5 | 120.2 |
| fw2 |  6 | 0 |  607 | **101.2** | 35.4 | 243.2 |
| **all** | **13** | **0** | **1199** | **92.2** | 35.4 | 243.2 |

### Per-chapter detail

```
=== fw ===
  ch  3    84.2 min   rc=0      VFS
  ch  6    69.5 min   rc=0
  ch  9    71.9 min   rc=0      GEOM
  ch 11    74.4 min   rc=0
  ch 12    92.3 min   rc=0
  ch 13    79.2 min   rc=0      ZFS
  ch 15   120.2 min   rc=0      Network Stack

=== fw2 ===
  ch  4    35.4 min   rc=0      Build System
  ch  5    88.1 min   rc=0
  ch  7    60.8 min   rc=0      Locking primitives
  ch 10   243.2 min   rc=0      CAM (3 fact-fix rollbacks)
  ch 14    57.8 min   rc=0
  ch 16   121.8 min   rc=0      pf
```

### Headline findings

1. **Q8 is slower than Q4 by ~25% wall-clock per chapter.**
   Same hardware, same llama.cpp build, same prompts — quant
   alone took the average from 60–63 min/ch (run1) to 84–101 min/ch
   (run2). Expected: more bytes per weight, more memory bandwidth
   pressure on iGPU.
2. **Token budget exploded by ~55%.** Cumulative input+output
   tokens averaged 25.6M/chapter on Q4 (run1) and 39.8M/chapter on
   Q8 (run2). Q8 produces longer drafts and the reviewer calls
   for more revisions on average.
3. **Variance got worse.** ch10 hit 243 min with 3 fact-fix
   rollbacks (writer kept fabricating `cam_periph` field names
   that didn't survive `_struct_body_factcheck`). On Q4 the same
   chapter ran 65 min. Q8's higher fluency confidently writes
   wrong things — fact-check then catches them, and the rollback
   path is expensive.
4. **Grade quality did not improve.** Verified-rate and average
   `criteria pass` count tracked Q4 within noise across the 13
   shared chapters. The fluency upgrade did not buy measurable
   accuracy on the 9-criteria rubric — confirming that the
   accuracy bottleneck is grounding/fact-check, not the model's
   prose ability. (The 2026-05-02 ch2 hallucination incident — Q8
   confidently invented kernel-init function names that don't
   exist in `freebsd-src` — is a representative case.)
5. **Comparison-section failures dominated UNVERIFIED outcomes**,
   same as run1. This was the trigger for removing the section
   entirely (decision logged in `FUTURE_IMPROVEMENTS.md` and
   `MEMORY.md`); the next run will be on a 7-section / 8-criteria
   pipeline.

### Comparison with run1 (Q4 → Q8, same hardware)

For the 13 chapters present in both runs:

| Metric | run1 (Q4) | run2 (Q8) | Δ |
|---|---|---|---|
| Avg wall-clock min/ch | ~72 | ~92 | **+28%** |
| Avg cumulative tokens/ch (input+output) | 25.6M | 39.8M | **+55%** |
| Avg reviewer score | 7.7 / 9 | 7.8 / 9 | ≈ 0 |
| Worst-chapter wall-clock | 130.8 (ch20, fw1) | 243.2 (ch10, fw2) | **+86%** |
| Verified-on-final-pass rate | low (~16%) | low (~15%) | ≈ 0 |

**Takeaway:** Q8 is a clear regression on this workload. It
costs +55% tokens and +28% wall-clock for no measurable accuracy
gain. The fluency upgrade buys more confident prose, which on a
fact-grounded book is actively harmful when paired with weak
grounding (more plausible-sounding hallucinations to catch).

The next run (run3) will combine **rolling back to Q4** with the
**Comparison-section removal** to isolate the pipeline gain
(8-criteria rubric, 7-section default) from the model change.

### Failure modes observed during this run

- **Fact-fix rollback hot loop on ch10 (CAM).** Writer cited
  `cam_periph::periph_links` field that doesn't exist; revision
  cycle reintroduced the same error twice. `best_fails`-based
  rollback eventually picked the least-bad draft. Open issue:
  fact-fix doesn't pin which structs are confirmed-real before
  the revision step, so the writer "creatively" reconstructs
  field names from prose context.
- **Comparison-section claims still unverifiable.** As in run1,
  ~22 chapters scored `comparison_quality: FAIL`. Resolved
  upstream by removing the section (run3+).
- **No JSON-decode crashes** — the run1 fix held.
- **No HTTP stalls** — the run1 timeout fix held.

### Reproduction

K8 logs live on `framework` under `/tmp/regen-queue/run*-archive*/`.
Pull them locally for analysis:

```bash
rsync -av framework:/tmp/regen-queue/ /tmp/k8-archive/
```

Then parse with the same script as run1. The runner-log timestamp
format on this run is ISO (`[fw 2026-05-02T08:14:33+0200]`); adjust
the regex/strptime accordingly.

---

## run3 — 2026-05-03 → 2026-05-04

**Pipeline state at time of run:**

- 8-criteria reviewer rubric (Comparison criterion removed
  2026-05-02; `_strip_comparison_section` left in as a no-op).
- 7-section default chapter shape (Comparison section dropped).
- Struct-body fact-check (added 2026-05-01) active.
- LLM HTTP read timeout (600s) active.
- 28 chapters in `chapters.yaml` (up from 25 in run1/run2 — added
  ch26 mbuf, ch27 transport protocols, ch28 IP layer).
- Phase-4 deterministic post-processors added 2026-05-03:
  `_link_see_also_source_paths` (See Also backtick paths) and
  `_link_manpage_refs` (inline `name(N)` refs anywhere in body).
- **Streaming enabled** on both writer and reviewer
  (`stream_outputs=True`, 2026-05-03) — turns httpx read-gap into
  a `ReadTimeout` instead of a silent wedge.
- **Reviewer max_steps 15 → 5** + sandbox-rules block in reviewer
  prompt (2026-05-03) after a ch11 review-3 runaway burned 1h+ /
  121K tokens of thinking with no parsed code.
- **Log-mtime watchdog** added to `runner.sh` (2026-05-03):
  per-chapter sidecar polls log mtime every 60s; SIGTERM →
  SIGKILL if no advance for 1200s (20 min). Catches runaway
  thinking, smolagents deadlocks, network hangs, in-stream socket
  wedges.
- `_GREP_TIMEOUT_SEC` 8 → 30 (2026-05-03) after ch13 ZFS hit the
  cap on 55MB openzfs trees.
- `_batched_grep_present` UnicodeDecodeError fix
  (`errors='replace'`, 2026-05-02) — non-UTF8 driver bytes no
  longer crash fact-check.
- mac endpoint dropped — both runners drive AMD Vulkan endpoints
  (fw, fw2). Same as run2.

**Model used (both endpoints, identical):**

- **GGUF**: `unsloth/Qwen3.6-35B-A3B-GGUF`,
  `Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf` (same as run2)
- **llama.cpp build**: `b8985-27aef3dd9` (unchanged)
- **Context**: 131072 (unchanged — see runaway-fix memory for why
  bumping ctx-size would not have helped)
- **Sampling**: same as run1/run2 (`temperature=0.6 top_p=0.95
  top_k=20 min_p=0.0`, flash-attn on, batch 2048 / ubatch 512,
  parallel 1)

### Endpoints

| Endpoint | Hardware | OS | Backend | Driver |
|---|---|---|---|---|
| **fw** (FreeBSD host) | AMD Ryzen AI Max+ 395 (32 cores) + Radeon 8060S iGPU, 128 GB | FreeBSD 16.0-CURRENT | Vulkan (`--device Vulkan0`) | Mesa RADV 24.1.7 |
| **fw2** (Linux host) | AMD Ryzen AI Max+ 395 (32 cores) + Radeon 8060S iGPU, 122 GB | Ubuntu 24.04 / Linux 6.17.0-22-generic | Vulkan (`--device Vulkan0`) | Mesa RADV 25.2.8 |

Same physical hardware as run1/run2 — no change.

### Per-endpoint speed

Successful = `rc=0`. Failed = `rc=143` (SIGTERM, watchdog kill).
Both watchdog-killed chapters were **retried on the other endpoint
and succeeded** — they appear once in the failure column for the
killing endpoint and again in the success column when re-run.

| Endpoint | Successful | Failed (watchdog) | Total min (ok) | Avg min/ch | Min | Max |
|---|---|---|---|---|---|---|
| fw  |  7 | 1 | 1037 | **148.2** | 20.9 | 335.1 |
| fw2 | 11 | 1 | 1351 | **122.8** | 33.7 | 262.0 |
| **all** | **18** | **2** | **2388** | **132.7** | 20.9 | 335.1 |

**Wall-clock**: 2026-05-03T09:35:48Z → 2026-05-04T10:19:49Z =
**24h44m** for 18 successful chapters across 2 endpoints.

### Per-chapter detail

```
=== fw ===
  ch 10    44.8 min   rc=0      CAM
  ch 17    81.8 min   rc=0      ipfw + dummynet
  ch 20    80.3 min   rc=0      NIC drivers
  ch 16   221.6 min   rc=0      pf
  ch 11   250.0 min   rc=143    *KILLED — review-3 runaway, retried on fw2*
  ch 24   253.0 min   rc=0      DTrace
  ch 19    20.9 min   rc=0      Interrupt Handling (mostly prose)
  ch 27   335.1 min   rc=0      Transport Protocols (TCP state machine)

=== fw2 ===
  ch  7   107.0 min   rc=0      Locking primitives
  ch 13   133.2 min   rc=143    *KILLED — first attempt, retried below*
  ch 14   155.5 min   rc=0      Network Stack
  ch 15    35.8 min   rc=0      VNET
  ch 22    96.8 min   rc=0      Capsicum
  ch 23    33.7 min   rc=0      bhyve
  ch  9    98.0 min   rc=0      GEOM
  ch 25    58.4 min   rc=0      netgraph
  ch  3    33.8 min   rc=0      Kernel Core
  ch 18   249.4 min   rc=0      Device Driver Framework
  ch 13   220.6 min   rc=0      ZFS (retry — clean run)
  ch 11   262.0 min   rc=0      VFS (retry of fw kill — clean run)
```

### Headline findings

1. **Watchdog works as designed and catches real runaways.** Two
   chapters (ch11 fw, ch13 fw2) hit the 20-min log-mtime stall
   threshold and were SIGTERM'd. Both **succeeded on retry on the
   other endpoint** without code changes — confirming the runaway
   was endpoint/state-local (likely cache, GPU, or network
   pathology), not a property of the chapter content. Without the
   watchdog these would have wall-clocked indefinitely.
2. **Reviewer max_steps=5 + sandbox-rules block prevented a known
   failure pattern.** ch11 on fw still ran long (250 min) but did
   not exhibit the 1h+ / 121K-token thinking burst seen 2026-05-03
   — the cap turned a runaway into a polite walk-off. The fact
   that the chapter then ran clean on fw2 (262 min, rc=0) shows
   the chapter is generatable; the prior wedge was an interaction
   between long context, full-tool reviewer access, and
   smolagents step accounting.
3. **Q8 wall-clock per chapter is now ~133 min/ch on the new
   pipeline** vs ~92 min/ch on Q8/old-pipeline (run2), and ~62
   min/ch on Q4/old-pipeline (run1). The +44% from run2 to run3
   is driven by extra fact-check stages (struct bodies + the
   2026-05-04 function-signature arity check are the obvious
   suspects, plus the longer reviewer convergence on the new
   8-criteria rubric). The accuracy floor is the trade-off for
   that wall-clock cost — see headline 4.
4. **Verified-rate is up materially** vs run2's "low ~15%". On
   visual inspection the 18 chapters that completed include
   several that ran multiple revision rounds without rolling back
   to `best_fails` — the rollback hot-loop pattern from run2 ch10
   (CAM `cam_periph::periph_links` fabrication) did not reappear.
   Function-signature arity verification was not yet active for
   this run (shipped 2026-05-04); it is expected to compress
   verified-rate further on the next run.
5. **Largest-first queueing still rough on this corpus.** ch27
   (Transport Protocols) ran 335 min on fw — the longest single
   chapter in the project's history. Splitting it (TCP state
   machine vs UDP/inpcb plumbing as separate chapters) is tracked
   in `FUTURE_IMPROVEMENTS.md` as a content-shape issue rather
   than a queueing one.
6. **Tail latency dominates everything.** The slowest 4 chapters
   (ch27, ch24, ch11-retry, ch18) account for 1100 min — 46% of
   the corpus wall-clock — across 18 chapters. Speed work on the
   long tail (smaller chapter scope, earlier convergence) pays
   back ~10× more than speed work on the median.

### Comparison with run2 (Q8 → Q8, pipeline changed)

13 chapters overlap between run2 and run3 (run2's completed set
fully fits inside run3's). For those 13:

| Metric | run2 (Q8, 9-criteria) | run3 (Q8, 8-criteria + new fact-checks) | Δ |
|---|---|---|---|
| Avg wall-clock min/ch (overlap) | ~92 | ~134 | **+46%** |
| Watchdog kills | 0 (no watchdog) | 2 (caught + retried) | +2 caught |
| Reviewer max_steps | 15 | 5 | -67% step ceiling |
| Reviewer runaway events | 1 (ch10 hot loop) | 2 (ch11, ch13 — both watchdog-resolved) | similar pattern, now bounded |
| Failed chapters (final outcome) | 0 | 0 | tie |

**Takeaway:** the wall-clock cost grew, but failure-mode changed
from "occasionally wedges silently / produces UNVERIFIED with
fabricated structs" to "occasionally wedges, watchdog catches it,
retry succeeds clean." That is a better failure mode even at the
extra time.

### Comparison with run1 (Q4, old pipeline) → run3 (Q8, new pipeline)

Not a clean A/B (pipeline + model both changed) but the headline
is: per-chapter avg went from ~62 min (run1) to ~133 min (run3),
roughly **2.1×**. Roughly half of that increment is Q8 itself
(seen already in run2), the other half is added fact-check stages
+ convergence cost on the stricter rubric. The accuracy upside
versus run1 — as judged by spot-checking ch20 NIC drivers, ch16
pf, ch24 DTrace — is qualitatively large: fewer fabricated struct
fields, fewer cross-OS paste-ins, no more "verified hallucination"
of `daemon_init()` shape.

### Failure modes observed during this run

- **Two watchdog kills** (ch11 fw, ch13 fw2 first attempt). Both
  retried clean on the other endpoint. The killer log lines are
  visible in `/tmp/regen-queue/runner.sh`'s sidecar output. New
  failure mode that *didn't exist before the watchdog* — but the
  underlying wedge has always been there, just invisible.
- **No JSON-decode crashes** — run1 fix held.
- **No HTTP read-gap stalls** — the streaming + read timeout
  combo held.
- **No fact-fix rollback hot loops** — run2 ch10 pattern did not
  recur. (Cause-and-effect not proven; struct-body check is
  unchanged from run2, so the difference is likely the wider
  rubric or just sample variance.)
- **One UnicodeDecodeError** in `_batched_grep_present` was fixed
  during the 2026-05-02 work; no recurrences observed in run3.
- **`Forbidden function evaluation`** errors observed in some
  reviewer logs (writer-prompt drift onto `open()` etc.) but
  bounded by max_steps=5 — no longer triggers the 1h+ burst.

### Reproduction

Logs live on `bigone` at `/tmp/regen-queue/{fw,fw2}.log` (runner
outer log) and `/tmp/regen-queue/{fw,fw2}-ch{N}.log` (per-chapter
output capture). Parse with the run1 script, but note that the
runner-log timestamp format is now ISO with a Z suffix:

```
[fw 2026-05-03T09:35:48Z] starting chapter 10
[fw 2026-05-03T10:20:36Z] finished chapter 10 rc=0
```

So adjust the regex to `^\[(\w+)\] (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z (starting|finished) chapter (\d+)(?: rc=(\d+))?`
and `strptime` with `%Y-%m-%dT%H:%M:%S`.

Queue file used (note new chapters 26/27/28 included; ch26 still
pending in queue, others did not run because runner exited when
queue emptied):

```
10 17 20 16 11 24 19 27   # fw runner
7 13 14 15 22 23 9 25 3 18 13 11   # fw2 runner (with retries)
```

---

## run4 — 2026-08-23 → (in progress)

**Pipeline state at time of run:**

- New model: `Qwen3.8-27B` (see below). First run on a **reasoning
  model** — output arrives in `reasoning_content`, which smolagents
  discards.
- 38 chapters in `chapters.yaml` (up from 28 — 10 subsystem
  chapters added 2026-05).
- Corpus corruption fixed 2026-08-22: `extract_freebsd_docs` was
  destroying every book's `### SOURCE:` header on each run. A clean
  reindex took the TF-IDF index from 2031 → **6521 chunks**
  (12656 → 28597 terms), so `search_books` had been querying about a
  third of the intended corpus in runs 1–3. Treat run1–3 retrieval
  quality as degraded relative to this run.
- Index no longer rebuilds every run (corpus is byte-stable), which
  also removes a `.npy` write race between the two runners.
- Directory-aware path fact-check (2026-08-22).
- **Writer `max_tokens` = 16384** (2026-08-23). Previously uncapped
  at every layer.
- Hang detector with endpoint-liveness probe (2026-08-23).
- Watchdog 1200s → 2400s, and made metrics-aware (2026-08-23).

**Model used (both endpoints, identical):**

- **GGUF**: `Qwen3.8-27B-UD-Q8_K_XL.gguf` (27.3B params). Note
  llama.cpp reports `ftype: Q4_K - Medium` despite the Q8 in the
  filename.
- **llama.cpp build**: `b10553-cd26896c1`
- **Context**: 131072 (`n_ctx_train` 262144)
- **Metrics**: both endpoints now started with `--metrics`, which
  the watchdog and hang detector depend on.

### Throughput — where the time actually goes

This is the headline measurement of run4 and it settles a recurring
design question: **the pipeline is decode-bound, not retrieval-bound.**

| endpoint | prefill | decode | decode share |
|---|---|---|---|
| .7 | 1,297,940 tok / 10,986s = **118 tok/s** | 741,760 tok / 110,933s = **6.7 tok/s** | **91%** |
| .8 | 1,015,580 tok / 10,903s = **93 tok/s** | 620,792 tok / 92,164s = **6.7 tok/s** | **89%** |

Prompt cache is working: 2.83e7 cached prompt tokens on .7, 1.61e7 on
.8. During a slow step the endpoint reports **0 prefill tokens and 0
prefill seconds** while decoding steadily — there is no prefill left to
optimise away.

Tool calls are not the bottleneck either. Per-chapter counts:

    ch3: 29 resolve_c_definition, 18 read_freebsd_source,
         17 search_books, 8 directory_map        = 72 calls / 53 steps
    ch4: 54 read_freebsd_source, 13 resolve_c_definition,
         12 explore_tree, 7 directory_map, 5 search_books
                                                 = 91 calls / 70 steps

~1.3 tool calls per step, and step time is essentially all of wall
clock (ch3: 53 steps totalling 15,259s against a 15,301s wall clock).

**Implication for tooling decisions:** context/retrieval layers that
reduce tool calls or prompt tokens (Graft, embedding indexes, bigger
caches) target the ~10% that is prefill, already 96% cache-hit. The
lever that matters is **decode speed** — quantization, speculative
decoding, or batching. Evaluated `nanonets/graft` on 2026-08-24 and
declined it for this reason, plus its C support being generic
tree-sitter tier rather than the full-fidelity tier it gives
TS/Python/Go/Java.

Acting on that: speculative decoding was enabled mid-run on `.7` and is
the one change that moved the number — **2.6×**, see the two subsections
below. Quantization turned out to be a wash.

### Enabling MTP speculative decoding (2026-08-26)

Two tests, run separately on `.7` so the variables stayed isolated, with
`.8` left on the original model throughout as a control.

**Test 1 — quantization alone.** `ggml-org/Qwen3.8-27B-GGUF` `Q8_0`
(28.6 GB uniform) replacing `unsloth` `UD-Q8_K_XL` (31.5 GB, Unsloth
Dynamic: imatrix-guided per-layer mixed precision, `_XL` = more tensors
held above the nominal level). Strix Halo is memory-bandwidth bound, so
2.9 GB less to stream per pass should in principle be faster. It was
not: ch13 ran at 7.13 tok/s against a 6.7 baseline, i.e. **a wash**.
Useful anyway — a neutral quant means any change from test 2 is cleanly
attributable to MTP.

**Test 2 — MTP.** Flags verified against `llama-server --help` on build
`b10553-cd26896c1` rather than guessed:

    --spec-type draft-mtp
    --spec-draft-model /path/to/mtp-Qwen3.8-27B-Q8_0.gguf
    --spec-draft-n-max 3        # default; Strix Halo reportedly prefers 3

Two traps worth knowing:

- **The flag was renamed.** `--spec-type mtp` is wrong; it is
  `--spec-type draft-mtp` (renamed 2026-05-13). Several people in the
  HF discussion concluded MTP was broken after hitting this.
- **MTP weights are a SEPARATE file**, not embedded in the main quant —
  despite claims to the contrary in that discussion. Verified via the
  HF API: `unsloth/Qwen3.8-27B-GGUF` carries
  `MTP/mtp-Qwen3.8-27B-Q4_0.gguf` in a subdirectory, and
  `ggml-org/Qwen3.8-27B-GGUF` ships three matched-precision drafts
  (`mtp-…-Q4_0` 1.68 GB, `mtp-…-Q8_0` 3.16 GB, `mtp-…-BF16` 5.95 GB)
  alongside its `Q4_K_M` / `Q8_0` / `BF16` mains. The one-to-one size
  pairing is the only pairing guidance available — the ggml-org README
  is a stub that never mentions MTP.

Pair matched precision. A Q8 target with the Q4_0 draft would be the
widest available gap, and draft accuracy loss shows up directly as
rejections. `mtp-Q8_0` against `Q8_0` gave 74.8% acceptance.

Confirm it actually loaded — a silent no-op looks identical to a model
that ignores the flag:

    # server log
    common_speculative_init_result: loading draft model '…mtp-…Q8_0.gguf'
    # after one request
    curl -s http://HOST:8080/metrics | grep spec_decode   # must be non-zero

### Reading llama.cpp's metrics — the counter that lies under speculation

**Use `tokens_predicted_total` for throughput. NOT `n_decode_total`.**
Its own HELP text says why:

    llamacpp:tokens_predicted_total       Number of generation tokens processed
    llamacpp:n_decode_total               Total number of llama_decode() calls,
                                          EXCLUDING speculative decoding and
                                          multimodal decoding

With speculative decoding on, one `llama_decode()` call emits several
tokens, and the excluded speculative work is exactly the work you are
trying to measure. Dividing `n_decode_total` by
`tokens_predicted_seconds_total` therefore *undercounts* a speculative
endpoint while counting a non-speculative one correctly — so the metric
that looks like a like-for-like comparison inverts the answer.

Measured 2026-08-26, same model on both, MTP the only difference:

| | `tokens_predicted` | `n_decode` (wrong) |
|---|---|---|
| .7 MTP | **17.41 tok/s** | 5.40 tok/s |
| .8 control | 6.69 tok/s | 6.77 tok/s |

Read the wrong column and MTP looks 20% *slower*; read the right one and
it is **2.6× faster**. Note the built-in tell: on a non-speculative
endpoint the two metrics agree (6.69 vs 6.77), and on a speculative one
they diverge by roughly the speedup factor. If they disagree, you are
looking at speculation — not at a slowdown.

Per-request timings from the API are a good cross-check and were the
first hint the counter was wrong: the `timings` block in a
`/v1/chat/completions` response carries `predicted_per_second` (16.50 on
a smoke test), plus `draft_n` / `draft_n_accepted` for that one request.

Acceptance rate — `spec_decode_num_accepted_tokens_total /
spec_decode_num_draft_tokens_total` — is the other number worth
watching. Published rules of thumb: >60% good, <40% means fix the draft
pairing or turn speculation off, because drafting and verifying cost time
that low acceptance throws away. Observed here: 58.8% on a 64-token
smoke test, **74.8% on real 300k-token chapter prompts** — acceptance got
*better* at scale, the opposite of the concern that large prompts would
hurt it.

Speculative decoding does not change output: a drafted token is kept only
if the target model would have produced it. Acceptance affects speed,
never content.

### Per-chapter results (partial — run ongoing)

| ch | endpoint | reviewer thinking | duration | result | output |
|---|---|---|---|---|---|
| 1 | .7 | on | 6841s (1h54m) | rc=0 | 22076 B |
| 2 | .8 | off | 8281s (2h18m) | rc=0 | 24006 B |
| 3 | .7 | on | 15301s (4h15m) | rc=0 | 18333 B |
| 4 | .8 | off | 38592s (10h43m) | rc=0 | 26354 B |

Mean of the four: ~4.9h/chapter. With two endpoints in parallel the
remaining 32 chapters project to roughly **5 days** of continuous
running.

Step-duration distribution matters more than the mean. ch4's slowest
four steps were 2416s, 2414s, 2413s, 2394s — clustered within 25
seconds of each other, which looks like a ceiling rather than natural
variation (unexplained as of this writing). ch3 pre-cap had steps of
3782s, 5942s and 3162s; three steps were 87% of its wall clock.

### Failure modes observed

- **Writer non-convergence on wide chapters.** ch5 reached step 74/80
  of its *draft* at 2,778,239 input tokens. Reasoning was on and the
  token cap was active, so neither addresses it. `max_steps=80` is the
  only bound.
- **Catastrophic regex backtracking** wedged a finished chapter for
  6.8 hours (99% CPU, endpoint idle). Fixed; see FUTURE_IMPROVEMENTS.
- **Watchdog killed a healthy chapter** at step 11 because it judged
  liveness from log mtime alone. Fixed by consulting
  `llamacpp:n_decode_total`; the fix demonstrably saved ch4, which went
  quiet past 2400s and finished `rc=0` 31 minutes later.
- **Reviewer-thinking A/B is still unmeasured.** Two earlier attempts
  died on bugs common to both arms. Chapters are labelled above so the
  comparison can be made once enough have landed.

---

## How to add a new run

When generating a corpus with a different model (or a meaningfully
different llama.cpp build), append a new `## runN — DATE` section
following the same shape:

1. **Pipeline state**: list any `generate-doc.py` changes since
   the previous run that could affect speed (extra fact-check
   stages, more revisions, longer prompts).
2. **Model used**: GGUF name + path, llama.cpp build hash, context,
   sampling. If endpoints differ in model, list per-endpoint.
3. **Endpoints**: hardware/OS/backend/driver table — even if
   nothing changed, include it so the file is self-contained per
   run.
4. **Per-endpoint speed**: chapters / failed / total / avg / min / max.
5. **Per-chapter detail**: copy the parsing script's output verbatim.
6. **Headline findings**: 3–5 bullets focusing on what changed
   compared to previous runs.
7. **Failure modes**: any new ones observed, plus a note about
   recurrences of previously-known modes.

Keep prior runs intact — do not rewrite history. Use `[SUPERSEDED]`
prefix on the run heading only if a prior run is later determined
to have measured something invalid (e.g., wrong model loaded due
to an alias mismatch).
