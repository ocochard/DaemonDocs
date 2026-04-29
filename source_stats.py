"""Mine generate-doc.py logs for tool-call statistics.

Counts how often each tool is invoked, which source files get read most,
which directories the writer explores, and what books queries it issues.
Designed to be re-run after each batch of chapter generations to see
how the writer's reach is evolving.

Usage:
    python3 source_stats.py                  # default: scan /tmp/*.log
    python3 source_stats.py path1 path2 ...  # explicit log files

This is a temporary tool — once `run_chapter` grows a per-chapter
source-usage summary (see FUTURE_IMPROVEMENTS.md), this script can be
retired. The LLM tool grammar it scans for is documented in
`generate-doc.py` under the **Smolagents tools** banner.
"""
import glob
import os
import re
import sys
from collections import Counter, defaultdict

# Files scanned when no CLI args are given. The patterns cover:
#   - validation regen logs (fw1-validation.log, fw2-validation.log)
#   - parallel batch logs (fw1-batch*.log, fw2-batch*.log, fw2-ch*.log)
#   - per-wave / per-chapter logs (wave*-{fw1,fw2,mac}-ch*.log)
DEFAULT_GLOBS = [
    "/tmp/fw[12]-*.log",
    "/tmp/wave*.log",
]


def default_logs():
    found = []
    seen = set()
    for pat in DEFAULT_GLOBS:
        for path in sorted(glob.glob(pat)):
            if path not in seen and os.path.isfile(path):
                seen.add(path)
                found.append(path)
    return found


LOGS = sys.argv[1:] if len(sys.argv) > 1 else default_logs()

chapter_re = re.compile(r"^Generating chapter (\d+): (.*?)$", re.MULTILINE)

# Tool calls in CodeAgent step output: print(toolname(arg=...))
read_re = re.compile(r"""read_freebsd_source\(\s*path\s*=\s*['"]([^'"]+)['"]""")
search_re = re.compile(r"""search_books\(\s*query\s*=\s*['"]([^'"]+)['"]""")
dirmap_re = re.compile(r"""directory_map\(\s*path\s*=\s*['"]([^'"]+)['"]""")
explore_re = re.compile(r"""explore_tree\(\s*path\s*=\s*['"]([^'"]+)['"]""")
resolve_re = re.compile(r"""resolve_c_definition\(\s*symbol\s*=\s*['"]([^'"]+)['"]""")

file_reads = Counter()
top_dir_reads = Counter()
search_queries = Counter()
dirmap_calls = Counter()
explore_calls = Counter()
resolve_calls = Counter()
per_tool_total = Counter()
per_chapter_tools = defaultdict(Counter)

for log in LOGS:
    if not os.path.exists(log):
        continue
    with open(log, "r", errors="replace") as f:
        text = f.read()

    chapter_matches = list(chapter_re.finditer(text))
    if not chapter_matches:
        segments = [(os.path.basename(log), text)]
    else:
        segments = []
        for i, m in enumerate(chapter_matches):
            start = m.end()
            end = chapter_matches[i + 1].start() if i + 1 < len(chapter_matches) else len(text)
            segments.append((m.group(2).strip(), text[start:end]))

    for ch_title, seg in segments:
        for path in read_re.findall(seg):
            file_reads[path] += 1
            per_chapter_tools[ch_title]["read_freebsd_source"] += 1
            per_tool_total["read_freebsd_source"] += 1
            parts = path.split("/")
            if len(parts) >= 2 and parts[0] == "sys":
                top_dir_reads[f"sys/{parts[1]}"] += 1
            elif parts:
                top_dir_reads[parts[0]] += 1
        for q in search_re.findall(seg):
            search_queries[q[:80]] += 1
            per_chapter_tools[ch_title]["search_books"] += 1
            per_tool_total["search_books"] += 1
        for d in dirmap_re.findall(seg):
            dirmap_calls[d] += 1
            per_chapter_tools[ch_title]["directory_map"] += 1
            per_tool_total["directory_map"] += 1
        for d in explore_re.findall(seg):
            explore_calls[d] += 1
            per_chapter_tools[ch_title]["explore_tree"] += 1
            per_tool_total["explore_tree"] += 1
        for s in resolve_re.findall(seg):
            resolve_calls[s] += 1
            per_chapter_tools[ch_title]["resolve_c_definition"] += 1
            per_tool_total["resolve_c_definition"] += 1

n_logs = sum(1 for l in LOGS if os.path.exists(l))
total = sum(per_tool_total.values())
print(f"=== Tool usage across {n_logs} log files ===")
print(f"Total tool invocations: {total}")
print()
print(f"{'Tool':<25} {'Calls':>8} {'%':>6}")
print("-" * 41)
for tool, n in sorted(per_tool_total.items(), key=lambda x: -x[1]):
    pct = 100.0 * n / total if total else 0
    print(f"{tool:<25} {n:>8} {pct:>5.1f}%")
print()

print("=== Top 25 most-read source files ===")
for path, n in file_reads.most_common(25):
    print(f"  {n:>4}  {path}")
print()

print("=== Reads grouped by top-level subsystem dir ===")
for d, n in top_dir_reads.most_common(25):
    print(f"  {n:>4}  {d}/")
print()

print("=== directory_map paths (top 15) ===")
for d, n in dirmap_calls.most_common(15):
    print(f"  {n:>4}  {d}")
print()

print("=== resolve_c_definition top symbols (top 25) ===")
for s, n in resolve_calls.most_common(25):
    print(f"  {n:>4}  {s}")
print()

print("=== search_books queries (top 15) ===")
for q, n in search_queries.most_common(15):
    print(f"  {n:>4}  {q}")
print()

print("=== Per-chapter tool totals ===")
for ch, counter in sorted(per_chapter_tools.items()):
    parts = ", ".join(f"{t}={c}" for t, c in sorted(counter.items()))
    total_ch = sum(counter.values())
    print(f"  [{total_ch:>3}]  {ch}: {parts}")
