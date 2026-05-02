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

- `framework`  → `http://192.168.100.7:8080/v1`
- `framework2` → `http://192.168.100.136:8080/v1`

Set `OPENAI_BASE_URL` to one of those and `OPENAI_MODEL=Qwen3.6-35B-A3B-UD-Q8_K_XL`
(the current model served at both). Do not SSH into the endpoints
to launch jobs. See `CODE_MAP.md` "Execution topology."

**Do not commit or push.** The user manages git themselves.
Suggest commit messages as text only.
