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

Set `OPENAI_BASE_URL` to one of those and `OPENAI_MODEL=Qwen3.8-27B-UD-Q8_K_XL`
(the alias served at both, as of 2026-09-01). Do not SSH into the
endpoints to launch jobs. See `CODE_MAP.md` "Execution topology."

**`curl -s <endpoint>/models` cannot tell you which model is loaded** —
it returns only llama-server's `--alias`, and the launcher script
deliberately gives two different recipes the *same* alias so that
swapping one for the other needs no change to `OPENAI_MODEL`. As of
2026-09-01 both `qwen38-mtp` (Q8_0 dense + matched-precision MTP draft
head, the production recipe) and `qwen38-q8` (plain Q8_K_XL, no draft
head) answer `Qwen3.8-27B-UD-Q8_K_XL`. A matching alias therefore proves
only that the endpoint is up.

To identify the actual recipe, read the server's argv on the endpoint
host and look at `--model` and `--spec-draft-model`; `--spec-type
draft-mtp` plus a draft model means MTP, its absence means plain Q8.
Read-only inspection like this is not "launching a job" and is fine.

**The model is a reasoning model** and both agents keep reasoning
enabled. Do not "optimize" it away: it is ~3x faster per call and far
more expensive per chapter, because the writer substitutes tool calls
for deliberation and stops converging (with thinking off it hit
`max_steps` twice and called one symbol 24 times; with it on, zero and
never above 2x). The post-mortem is in `FUTURE_IMPROVEMENTS.md`.

**Do not commit or push.** The user manages git themselves.
Suggest commit messages as text only.
