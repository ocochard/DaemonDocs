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

**Execution:** the script runs from `framework` (fw1). `framework2`
(192.168.100.136) is only an LLM endpoint — never SSH there to
launch jobs. See `CODE_MAP.md` "Execution topology."

**Do not commit or push.** The user manages git themselves.
Suggest commit messages as text only.
