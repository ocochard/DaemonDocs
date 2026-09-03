# DaemonDocs — for Claude Code

**Before editing `generate-doc.py`, read `CODE_MAP.md` in this
directory.** It points to the load-bearing functions and the
"where do I add X?" patterns. Don't re-derive the structure with
Explore — the map is faster and more accurate.

**Before changing pipeline behavior** (writer prompt, reviewer
prompt, run_chapter ordering, fact-check), also read the
"Pipeline quality issues observed in real runs" section of
`FUTURE_IMPROVEMENTS.md`. Several "obvious" changes have been
tried and broke specific things; the post-mortems are there.

**Execution:** the script runs from this host. The repo,
`~/freebsd-src`, `~/freebsd-doc`, and `~/books` all live here. LLMs
are remote llama-servers — used purely as compute, no repo / source
tree / script there:

- `framework`  → `$FW_URL`
- `framework2` → `$FW2_URL`

This is a public repo, so the LAN addresses are not written down here.
They are in the operator's global `~/.claude/CLAUDE.md` under "Host →
LAN endpoints"; substitute from there, or read them off a running
job with `pgrep -lf runner.sh`.

Set `OPENAI_BASE_URL` to one of those and `OPENAI_MODEL` to the alias the
endpoint actually serves (see below). Do not SSH into the endpoints to
launch jobs. See `CODE_MAP.md` "Execution topology."

**Each recipe has its own alias, so `curl -s <endpoint>/models` does
identify what is loaded.** The launcher (`llmsrv.sh`, outside this repo)
has two slots, and the alias is one-to-one with the slot:

| slot | weights | alias |
|---|---|---|
| `qwen38-mtp` (default) | ggml-org Qwen3.8-27B **Q8_0** + Q8_0 sidecar MTP draft head | `Qwen3.8-27B-Q8_0-MTP` |
| `qwen38-q8` | unsloth Qwen3.8-27B **Q8_K_XL**, no draft head | `Qwen3.8-27B-UD-Q8_K_XL` |

Note the two slots are different repos *and* different quants, not one
model with speculative decoding toggled. `qwen38-mtp` is the launcher
default; `qwen38-q8` is the spec-off fallback.

**You normally do not set `MODEL_ALIAS`.** `scripts/regen-runner.sh`
resolves it from the endpoint's own `/v1/models` at startup
(`scripts/regen-runner.sh:128-138`) and logs which source it used as
`model_alias=<alias>(endpoint|env|fallback-literal)`. Setting
`MODEL_ALIAS` explicitly still wins, and is only needed to force a
mismatch deliberately. Resolution happens once at startup, so an
endpoint restarted onto the other slot mid-run keeps receiving the
stale alias; the log line is where that shows up.

Earlier revisions of this file claimed the two recipes deliberately
shared one alias. They never did; that guidance was wrong and is the
reason `curl /models` was previously described as non-diagnostic.

The MTP draft path had a **known fault on `framework`
(FreeBSD/Mesa 26)**: a 2026-08-15 bench recorded a RADV GPUVM fault in
the `draft-mtp` dispatch path on a coin-flip of cold starts, where a
dead load makes throughput *worse* than spec-off. It reproduced with an
unrelated model, so it tracks the MTP code path rather than any one
GGUF, and it was clean on Ubuntu/Mesa 25. **It did not reproduce on
2026-09-03** (numbers below), on one cold start with a clean `dmesg`.
The original was intermittent, so one clean start is evidence, not
proof: re-read `/metrics` after a restart before trusting it.

**Neither `/props` nor `/v1/models` can confirm MTP is live** — both
report only the target model, never the draft head. The speculative
counters in `/metrics` are the only proof, and only after real
decoding: generate a few tokens, then read
`llamacpp:spec_decode_num_draft_tokens_total` and
`..._accepted_tokens_total`. Requires the server to run with
`--metrics`. Interpretation:

- `draft_tokens_total 0` → MTP is not loaded at all.
- `draft > 0, accepted 0` → the FreeBSD dead load. Restart, check
  `dmesg | grep gfxhub`, and fall back to `qwen38-q8` if it recurs.
- both nonzero → live. Measured 2026-09-03 on the Q8/Q8 pairing,
  against ~34% for the 2026-08-15 Q4 pairing:

  | endpoint | draft | accepted | acceptance | drafts | per-position |
  |---|---|---|---|---|---|
  | `framework` | 62 | 43 | **69.4%** | 16 | 14/11/10/8 |
  | `framework2` | 44 | 33 | **75.0%** | 11 | 10/9/7/7 |

  Small samples; the 69 vs 75 gap is noise. What matters is that both
  are far above the Q4 baseline and both decay monotonically across
  draft positions, which is what a healthy draft head looks like.

Reading the server's argv, log, or HTTP endpoints is read-only
inspection, not "launching a job", and is fine.

**The model is a reasoning model** and both agents keep reasoning
enabled. Do not "optimize" it away: it is ~3x faster per call and far
more expensive per chapter, because the writer substitutes tool calls
for deliberation and stops converging (with thinking off it hit
`max_steps` twice and called one symbol 24 times; with it on, zero and
never above 2x). The post-mortem is in `FUTURE_IMPROVEMENTS.md`.

**Do not commit or push.** The user manages git themselves.
Suggest commit messages as text only.
