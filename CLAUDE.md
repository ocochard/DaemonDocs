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

**Execution:** the script runs from this host (`bigone`). The repo,
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
(the current model served at both, as of 2026-08-22 — verify with
`curl -s <endpoint>/models` rather than trusting this line). Do not
SSH into the endpoints to launch jobs. See `CODE_MAP.md` "Execution
topology."

**The model is a reasoning model** and both agents keep reasoning
enabled. Do not "optimize" it away: it is ~3x faster per call and far
more expensive per chapter, because the writer substitutes tool calls
for deliberation and stops converging (with thinking off it hit
`max_steps` twice and called one symbol 24 times; with it on, zero and
never above 2x). The post-mortem is in `FUTURE_IMPROVEMENTS.md`.

**Do not commit or push.** The user manages git themselves.
Suggest commit messages as text only.
