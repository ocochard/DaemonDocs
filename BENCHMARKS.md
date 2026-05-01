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
