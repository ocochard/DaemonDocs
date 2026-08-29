#!/usr/bin/env python3
"""
FreeBSD Internals — Documentation Generator

Uses smolagents to produce README.md files throughout the FreeBSD
source tree. Each chapter is driven by chapters.yaml and the agent has
access to:
  - The FreeBSD source code  (read_freebsd_source)
  - A semantic search index of FreeBSD books (search_books)
  - The source tree structure (explore_tree)
  - Per-directory structured summaries (directory_map)

Usage:
  python3 FreeBSD/generate-doc.py                 # run all chapters
  python3 FreeBSD/generate-doc.py --chapter 2     # run only chapter 2 (1-based)
  python3 FreeBSD/generate-doc.py --index-only    # just build the book index
  python3 FreeBSD/generate-doc.py --force         # regenerate even if exists
  python3 FreeBSD/generate-doc.py --dry-run       # show what would happen
  python3 FreeBSD/generate-doc.py --reindex       # rebuild book index from scratch

Environment:
  FREEBSD_SRC      — root of FreeBSD git tree (default: $HOME/freebsd-src)
  BOOKS_DIR        — directory with PDF/CHM/EPUB books (default: $HOME/books)
  OPENAI_BASE_URL  — LLM endpoint (default: http://localhost:8080/v1)
  OPENAI_API_KEY   — API key (default: "none", suitable for local llama-server)
  OPENAI_MODEL     — model id / alias (default: qwen36-coder)
                     Each of the three OPENAI_* vars overrides one field
                     of MODEL_CONFIG independently.

Requirements:
  python3 -m pip install --user -r FreeBSD/requirements.txt
"""

import argparse
import faulthandler
import glob
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import yaml
from PyPDF2 import PdfReader
from smolagents import CodeAgent, OpenAIServerModel, Tool

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_ROOT = os.path.expandvars(
    os.environ.get("FREEBSD_SRC", "${HOME}/freebsd-src")
)
BOOKS_DIR = os.path.expandvars(
    os.environ.get("BOOKS_DIR", "${HOME}/books")
)
INDEX_DIR = SCRIPT_DIR / ".index"
CHAPTERS_FILE = SCRIPT_DIR / "chapters.yaml"

# FreeBSD documentation project (Handbook, articles, etc.)
FREEBSD_DOC = os.path.expandvars(
    os.environ.get("FREEBSD_DOC", "${HOME}/freebsd-doc/documentation/content/en")
)

# Adjust for your local LLM server.
# `model_id` is the short alias the local llama-server was started with
# (used for chat-completion routing). The real underlying model name —
# e.g. the GGUF filename — is fetched from /v1/models at runtime via
# resolve_real_model_name() and used in the provenance footer.
#
# Each field can be overridden independently via the standard OpenAI
# client env vars (OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL), so the
# same script can target a local llama-server (default) or any
# OpenAI-compatible endpoint without code edits.
MODEL_CONFIG = {
    "model_id": os.environ.get("OPENAI_MODEL", "qwen36-coder"),
    "api_base": os.environ.get("OPENAI_BASE_URL", "http://localhost:8080/v1"),
    "api_key": os.environ.get("OPENAI_API_KEY", "none"),
    # Hard ceiling on any single HTTP call to the LLM endpoint. Without
    # this, a wedged llama-server connection (server idle, TCP still
    # ESTABLISHED, no bytes flowing) wedges the chapter forever — the
    # python `openai` SDK has no inactivity timeout by default. Observed
    # 2026-05-01 on ch8: writer mid-step, llama-server at 0% CPU, log
    # file frozen for 6+ minutes with no recovery in sight.
    #
    # 600s is generous — a single chapter generation step usually
    # completes in 30–120s; the longest legitimate calls we've seen are
    # ~3 min. 600s gives 5× headroom while still bounding the worst
    # case. On timeout the SDK raises `APITimeoutError`, which the
    # existing try/except in run_chapter (around _run_agent) catches
    # and breaks the review loop — chapter writes UNVERIFIED instead
    # of stalling.
    "timeout": float(os.environ.get("DAEMONDOCS_LLM_TIMEOUT", "600")),
}

# ---------------------------------------------------------------------------
# Hang detector
# ---------------------------------------------------------------------------
#
# On 2026-08-22 a chapter that had ALREADY been written wedged in
# fact-checking: `_extract_fenced_function_defs` hit catastrophic regex
# backtracking and spun one CPU at 99% for 6.8 hours. Nothing caught it.
# The runner's log-mtime watchdog eventually killed the process, but a
# kill tells you only THAT something hung — diagnosing WHERE cost hours
# of bisecting extractors by hand.
#
# This installs a watchdog thread that dumps every thread's Python stack
# when the main thread stops checking in. Two properties matter:
#
#   * It uses `faulthandler.dump_traceback()`, which works even when the
#     main thread is stuck inside a C-level loop (a regex, a big sort).
#     A pure-Python timer cannot interrupt those; this can still print
#     from another thread.
#   * It is advisory. It never kills anything — it makes the failure
#     legible in the chapter log so the next hang takes minutes to
#     diagnose instead of hours. Killing stays the runner's job, where a
#     human set the policy.
#
# Usage: wrap any long phase in `heartbeat("label")` and call
# `beat()` inside its loops. Phases that never call `beat()` are still
# covered by the outer per-stage heartbeat in run_chapter.
# Quiet time before a stack dump. Must sit ABOVE legitimate LLM latency:
# a reasoning model can think for many minutes before emitting its first
# token, and a single measured legitimate step has reached 1164s. The
# first version used 300s and fired on a perfectly healthy draft — a
# detector that cries wolf is worse than none, because the next dump gets
# ignored. 1800s is ~1.5x the slowest step ever observed here; the
# pathological case it exists for ran for 6.8 HOURS, so there is no need
# to sit close to the legitimate ceiling.
_HANG_DUMP_AFTER_SEC = float(os.environ.get("DAEMONDOCS_HANG_DUMP_SEC", "1800"))
_hb_lock = threading.Lock()
_hb_last = time.monotonic()
_hb_label = "startup"
_hb_dumped_for: Optional[str] = None


def beat(label: Optional[str] = None) -> None:
    """Record progress. Cheap enough to call inside tight loops."""
    global _hb_last, _hb_label, _hb_dumped_for
    with _hb_lock:
        _hb_last = time.monotonic()
        if label is not None and label != _hb_label:
            _hb_label = label
            _hb_dumped_for = None  # new phase — allow one dump for it


class heartbeat:
    """Context manager marking a named phase for the hang detector."""

    def __init__(self, label: str):
        self.label = label
        self._prev: Optional[str] = None

    def __enter__(self):
        global _hb_label
        with _hb_lock:
            self._prev = _hb_label
        beat(self.label)
        return self

    def __exit__(self, *exc):
        beat(self._prev or "idle")
        return False


_DECODE_PROBE_GAP_SEC = 8.0


def _endpoint_decode_count() -> Optional[int]:
    """Read llama-server's cumulative decode counter, or None.

    None means "cannot tell" — no /metrics endpoint (llama-server needs
    `--metrics`), a non-llama.cpp backend, or an unreachable host. Callers
    must treat None as "assume NOT decoding" so the detector still fires.
    """
    base = MODEL_CONFIG["api_base"].rstrip("/")
    # /v1 is the OpenAI-compatible prefix; /metrics sits at the server root.
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    for line in body.splitlines():
        if line.startswith("llamacpp:n_decode_total "):
            try:
                return int(float(line.split()[1]))
            except (IndexError, ValueError):
                return None
    return None


def _endpoint_is_decoding() -> bool:
    """True only if the model demonstrably produced tokens just now.

    Samples `n_decode_total` twice. Conservative by construction: any
    uncertainty (metrics unavailable, counter flat) returns False so the
    hang detector still dumps. A local CPU spin with an idle endpoint —
    the 6.8-hour regex hang — reads as flat and is correctly reported.
    """
    first = _endpoint_decode_count()
    if first is None:
        return False
    time.sleep(_DECODE_PROBE_GAP_SEC)
    second = _endpoint_decode_count()
    if second is None:
        return False
    return second > first


def _hang_watchdog() -> None:
    """Dump stacks when the main thread goes quiet for too long."""
    # _hb_last is assigned below (to credit endpoint progress), which without
    # this declaration would make it local to the whole function and turn the
    # read at the top of the loop into an UnboundLocalError on the very first
    # tick. That is exactly what happened on ch17, 2026-08-26.
    global _hb_dumped_for, _hb_last
    while True:
        time.sleep(15)
        with _hb_lock:
            quiet = time.monotonic() - _hb_last
            label = _hb_label
            already = _hb_dumped_for == label
        if quiet < _HANG_DUMP_AFTER_SEC or already:
            continue

        # Before crying wolf, ask the endpoint whether the MODEL is busy.
        # A wall-clock threshold alone cannot tell "wedged" from "thinking
        # hard": llama-server decodes at ~7 tok/s here, so a long reasoning
        # block is many minutes of silence from a perfectly healthy run.
        # That miscalibration produced a false dump on 2026-08-23.
        #
        # `n_decode_total` is the right signal — it advances per token in
        # real time. (`tokens_predicted_total` looks tempting and is NOT:
        # it only rolls up when a request COMPLETES, so it sits frozen for
        # the entire generation you are trying to observe.)
        #
        # This only suppresses the dump when the endpoint is provably
        # working. Unreachable metrics, no --metrics flag, or a stalled
        # counter all fall through to the dump: the detector must fail
        # loud, since the bug it exists for (a local CPU spin with the
        # endpoint idle) shows up as exactly "no decode progress".
        if _endpoint_is_decoding():
            with _hb_lock:
                _hb_last = time.monotonic()  # credit the model's progress
            print(f"  [hang-detector] {quiet:.0f}s quiet in '{label}', but "
                  f"the endpoint is still decoding — not a hang, waiting.",
                  flush=True)
            continue

        with _hb_lock:
            _hb_dumped_for = label
        print(
            f"\n{'=' * 70}\n"
            f"  HANG DETECTOR: no progress for {quiet:.0f}s in phase "
            f"'{label}'\n"
            f"  Dumping thread stacks. The deepest frame in MainThread is\n"
            f"  where it is stuck. This is advisory — nothing is killed.\n"
            f"{'=' * 70}",
            flush=True,
        )
        # Print the main thread's stack explicitly first — that is the one
        # that is stuck, and faulthandler leads with the *calling* (i.e.
        # watchdog) thread, which is pure noise. Fall back to faulthandler
        # if frame introspection is unavailable for any reason.
        try:
            main_id = threading.main_thread().ident
            frame = sys._current_frames().get(main_id)
            if frame is not None:
                print("  MainThread (deepest frame last):", flush=True)
                for line in traceback.format_stack(frame):
                    print("    " + line.rstrip(), flush=True)
        except Exception:
            pass
        try:
            faulthandler.dump_traceback()
        except Exception as exc:  # never let the watchdog kill the run
            print(f"  (stack dump unavailable: {exc})")
        # Chapter logs are redirected to a file, so stdout is block-
        # buffered. Without this flush the dump can sit in the buffer
        # until the process is killed — i.e. exactly never, which is
        # the failure mode this whole thing exists to prevent.
        sys.stdout.flush()


def install_hang_detector() -> None:
    """Start the watchdog thread. Safe to call once from main()."""
    if _HANG_DUMP_AFTER_SEC <= 0:
        print("  [hang-detector] disabled (DAEMONDOCS_HANG_DUMP_SEC=0)")
        return

    # Report which mode we are in at startup. Without /metrics the
    # detector still works, but it degrades to a pure wall-clock timeout
    # that cannot tell "model thinking hard" from "process wedged" — and
    # a silent degradation is how you end up trusting a check that isn't
    # really checking. llama-server needs to be started with `--metrics`.
    count = _endpoint_decode_count()
    if count is None:
        print(f"  [hang-detector] on, {_HANG_DUMP_AFTER_SEC:.0f}s threshold "
              f"— WITHOUT endpoint liveness")
        print(f"                  {MODEL_CONFIG['api_base']} exposes no "
              f"/metrics; restart llama-server with --metrics to let the")
        print(f"                  detector tell a slow model apart from a "
              f"real hang (expect occasional false dumps until then).")
    else:
        print(f"  [hang-detector] on, {_HANG_DUMP_AFTER_SEC:.0f}s threshold, "
              f"endpoint liveness via /metrics (n_decode_total={count})")

    t = threading.Thread(target=_hang_watchdog, name="hang-watchdog",
                         daemon=True)
    t.start()


# Per-call generation ceiling for the WRITER, in tokens. 0 = unlimited.
#
# Nothing capped this before: not the script, not smolagents (no default),
# not llama-server (`n_predict` unset). A single step could therefore
# generate until it exhausted the 131072-token context — 5.4 hours at the
# ~6.8 tok/s this hardware sustains.
#
# It did. ch3 on 2026-08-23, 16 steps of a draft over 4h47m:
#
#     step  5   3782s   (~26k tokens)
#     step 11   5942s   (~40k tokens)
#     step 14   3162s   (~21k tokens)
#     ---------------------------------------------
#     3 steps = 12,886s of 14,863s total = 87% of wall clock
#
# The other 13 steps averaged 152s. Endpoint metrics rule out prefill as
# the cause: during a slow step the server reported 0 prefill tokens and
# 0 prefill seconds (96% prompt-cache hit rate) while decoding steadily at
# 6.8 tok/s. It is pure generation — almost certainly runaway reasoning,
# the same shape as the ch11 reviewer runaway (121K tokens of thinking
# with no parsed code) that forced reviewer max_steps 15->5. `max_steps`
# does not help here: the problem is ONE step, not many.
#
# 16384 is sized from measurement, not intuition. Normal steps run 30-180s
# = roughly 200-1200 tokens. Measured real chapters: README_internals.md
# 22076 B (~6.1k tokens), the ch2 loader chapter 24006 B (~6.7k), the ch3
# draft 20906 B (~5.8k). So the largest legitimate output observed is
# ~6.7k and this cap sits at ~2.4x that.
#
# It does NOT need to sit close to the legitimate ceiling to work: the
# runaways were 21k-40k tokens, 3-6x any real chapter, so a generous cap
# still cuts them at a third to a half while leaving ample headroom for a
# chapter larger than any produced so far. An earlier 8192 would also have
# worked, but left only ~20% margin over the biggest real draft — and the
# failure mode of a too-tight cap is bad: every draft truncates, best-draft
# tracking keeps shipping the least-bad partial, and nothing looks broken.
# Prefer headroom; the cap is insurance, not a tuning knob.
#
# Tradeoff, stated honestly: a truncated step can yield a malformed action
# and cost a retry. That is still far cheaper than 99 minutes, and
# `_looks_like_stub` plus best-draft tracking already absorb bad output.
# Set DAEMONDOCS_WRITER_MAX_TOKENS=0 to restore the old unbounded
# behaviour if a chapter turns out to need it.
WRITER_MAX_TOKENS = int(os.environ.get("DAEMONDOCS_WRITER_MAX_TOKENS", "16384"))

# Whether the WRITER gets the model's reasoning channel. Default ON.
#
# Disabling it looks like an obvious win per-call — 3x faster, and the
# discarded reasoning_content is pure waste since smolagents reads only
# `content`. It is not. Measured 2026-08-22, ch3 run twice concurrently,
# same model and prompt, writer thinking the ONLY variable:
#
#            stages                                 max_steps  repeat calls
#   OFF   draft->rev1->rev2, 3 reviews                   2      24x one symbol
#   ON    draft, 1 review (PASS first pass)              0      none above 2x
#
# Without the reasoning channel the writer substitutes tool calls for
# deliberation: it burns the whole 80-step draft budget re-reading the
# same files, produces a draft the reviewer rejects, then needs two
# revision rounds. The per-call saving is swamped by the extra calls.
#
# Token-total ratios are deliberately NOT quoted here. An earlier version
# of this comment claimed 13.8x; that comparison was contaminated by the
# _extract_fenced_function_defs backtracking hang, which held processes
# open for hours after the writer had finished, so the totals were
# measuring different things. The max_steps and repeat-call columns are
# what the conclusion rests on — they need no totals.
#
# This is why the first 2026-08-22 queue run died: ch3/ch4/ch5/ch6 all hit
# max_steps=80. Do not disable this again without re-running the two-arm
# experiment on a quiet machine — per-call latency is the wrong unit to
# optimize, total tokens to a finished chapter is the right one.
WRITER_THINKING = os.environ.get("DAEMONDOCS_WRITER_THINKING", "1") != "0"

# Whether the REVIEWER gets the model's reasoning channel. The reviewer is
# a separate question: it does multi-criteria
# judgment against injected ground truth (a plausible use for deliberation),
# but the ch11 runaway that forced max_steps 15->5 on 2026-05-03 was 121K
# tokens of thinking with no parsed code, i.e. this exact channel.
#
# Env-var gated so one endpoint can run reviewer-thinking ON and the other
# OFF against a shared chapter queue, giving a real A/B within a single run
# instead of an assumption. Set DAEMONDOCS_REVIEWER_THINKING=0 to disable.
REVIEWER_THINKING = os.environ.get("DAEMONDOCS_REVIEWER_THINKING", "1") != "0"

# Resolved at startup by resolve_model_provenance(); cached for the run.
RESOLVED_PROVENANCE: Optional[Dict[str, str]] = None


def _atomic_write(path: str, content: str, encoding: str = "utf-8") -> None:
    """Write `content` to `path` atomically.

    Strategy: write to a sibling tempfile in the same directory, then rename.
    `os.replace()` is atomic on POSIX and on Windows when the target is on the
    same filesystem — which is guaranteed here because the temp lives next to
    the destination.

    A Ctrl-C, crash, or full disk in the middle of a write therefore leaves
    either the previous file intact or no file at all — never a half-written
    one. This is critical for the corpus hash file, the TF-IDF index meta,
    the chapter outputs, and the navigation post-pass: a corrupted file in
    any of those would break the next run silently.
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of the tempfile on failure.
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _http_get_json(url: str, timeout: float) -> Optional[dict]:
    """Small helper: GET a URL and parse JSON. Returns None on any error."""
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {MODEL_CONFIG.get('api_key', 'none')}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def resolve_model_provenance(timeout: float = 3.0) -> Dict[str, str]:
    """Resolve the real model identity for the provenance footer.

    The `model_id` in MODEL_CONFIG is just the short alias passed to
    `llama-server --alias ...`. To get the actual model, we query
    `/props` — a llama-server-specific endpoint that exposes the real
    GGUF path and llama.cpp build info.

    Returns a dict with: model_name, build_info.
    Falls back to the alias on any failure so generation never blocks.
    """
    global RESOLVED_PROVENANCE
    if RESOLVED_PROVENANCE is not None:
        return RESOLVED_PROVENANCE

    alias = MODEL_CONFIG["model_id"]
    base = MODEL_CONFIG["api_base"].rstrip("/")
    # /props is hosted at the server root, not under /v1
    root = base[:-3] if base.endswith("/v1") else base

    name = alias
    build_info = ""

    props = _http_get_json(root + "/props", timeout)
    if props:
        # Prefer the GGUF filename stem — it's the most informative human-readable name.
        path = props.get("model_path") or ""
        if path:
            name = os.path.splitext(os.path.basename(path))[0]
        elif props.get("model_alias"):
            name = props["model_alias"]
        build_info = props.get("build_info", "") or ""
    else:
        # Fall back to OpenAI-compatible /v1/models — only exposes the alias on llama-server,
        # but useful for other backends.
        models = _http_get_json(base + "/models", timeout)
        if models and models.get("data"):
            first = models["data"][0]
            if isinstance(first, dict) and first.get("id"):
                name = first["id"]

    if name == alias:
        print(f"  [provenance] could not resolve real model name, using alias '{alias}'")

    RESOLVED_PROVENANCE = {"model_name": name, "build_info": build_info}
    return RESOLVED_PROVENANCE


def _provenance_footer() -> str:
    """Markdown footer recording which LLM produced the document.

    Inserted at the bottom of every generated chapter so a reader can tell
    at a glance which model wrote it and when.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prov = resolve_model_provenance()
    build = f" (llama.cpp build `{prov['build_info']}`)" if prov.get("build_info") else ""
    return (
        "\n\n---\n\n"
        "> _Generated by [DaemonDocs](https://github.com/ocochard/DaemonDocs) "
        f"on {ts} using model `{prov['model_name']}`{build}. "
        "AI-generated content — verify against source before relying on it._\n"
    )


# --- Optional sysctl fact-check via codebase-memory-mcp --------------------
#
# Sysctl OID paths (`vm.pmap.pde.mappings`) are the one class of symbol
# grep CANNOT verify: the dotted string exists nowhere in the source. It is
# assembled at compile time from a chain of `SYSCTL_NODE`/`SYSCTL_INT`/...
# macros whose leaves are bare identifiers (`OID_AUTO, mappings, ...`) under
# parent OID nodes (`_vm_pmap_pde`). To know `vm.pmap.pde.mappings` is real
# you must parse the macro tree and walk the parent chain — which the
# codebase-memory-mcp indexer does, exposing each reconstructed OID as a
# `Sysctl` graph node keyed by its full dotted `path`.
#
# This makes the graph an OPTIONAL fact-check backend, not a dependency:
# when the `codebase-memory-mcp` binary and a ready index for the source
# tree are both present, `_verify_sysctls_via_graph` verifies claimed OIDs
# against it; otherwise the check silently no-ops (returns "nothing missing")
# after printing a single warning. Everything else in the pipeline — and
# every other fact-check verifier — is grep-based and works unchanged
# without the graph. See CODE_MAP.md "Optional sysctl fact-check".
SYSCTL_GRAPH = {
    # The `codebase-memory-mcp` executable. Its `cli <tool>` subcommand runs
    # one graph query and exits, so we shell out per OID (results are cached
    # in `_SYSCTL_GRAPH_CACHE`, so re-runs within a session are free).
    "bin": os.environ.get("CODEBASE_MEMORY_MCP_BIN", "codebase-memory-mcp"),
    # The indexed project name. codebase-memory derives it from the repo
    # path: `/usr/home/olivier/freebsd-src` -> `usr-home-olivier-freebsd-src`.
    # Override with SYSCTL_GRAPH_PROJECT if your index is named differently.
    "project": os.environ.get(
        "SYSCTL_GRAPH_PROJECT",
        SRC_ROOT.strip("/").replace("/", "-"),
    ),
    "timeout": float(os.environ.get("SYSCTL_GRAPH_TIMEOUT", "30")),
}

# Tri-state cache for the availability probe: None = not yet probed,
# True/False = probed result. The probe runs at most once per process.
_SYSCTL_GRAPH_OK: Optional[bool] = None


def _sysctl_graph_available() -> bool:
    """Return True iff the codebase-memory-mcp graph can verify sysctls.

    Checks, once per process (result cached in `_SYSCTL_GRAPH_OK`):
      1. the `codebase-memory-mcp` binary is on PATH / at the configured path,
      2. it reports the configured project's index as `status: ready`.

    On any failure prints ONE warning explaining that sysctl OID
    verification is disabled and how to enable it, then returns False so
    `_verify_sysctls_via_graph` degrades to a no-op. This is the
    "graph optional" contract: the pipeline never blocks on it.
    """
    global _SYSCTL_GRAPH_OK
    if _SYSCTL_GRAPH_OK is not None:
        return _SYSCTL_GRAPH_OK

    bin_path = shutil.which(SYSCTL_GRAPH["bin"]) or (
        SYSCTL_GRAPH["bin"] if os.path.isfile(SYSCTL_GRAPH["bin"]) else None
    )
    if not bin_path:
        print(
            f"  [sysctl fact-check] disabled: '{SYSCTL_GRAPH['bin']}' not found. "
            f"Sysctl OID names (kern.*, vm.*, …) will NOT be verified — they "
            f"are the one symbol class grep cannot check. To enable, install "
            f"codebase-memory-mcp and index the source tree, or set "
            f"CODEBASE_MEMORY_MCP_BIN."
        )
        _SYSCTL_GRAPH_OK = False
        return False

    # Confirm the project index exists and is ready.
    ready = False
    try:
        proc = subprocess.run(
            [bin_path, "cli", "--json", "index_status",
             "--project", SYSCTL_GRAPH["project"]],
            capture_output=True, text=True, errors="replace",
            timeout=SYSCTL_GRAPH["timeout"],
        )
        # `cli --json` emits log lines on stderr and the JSON payload on
        # stdout; the payload is the last line starting with '{'.
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            payload = json.loads(line)
            text = ""
            if isinstance(payload.get("content"), list) and payload["content"]:
                text = payload["content"][0].get("text", "")
            # index_status ready-ness can appear either as a JSON field in
            # the wrapped text or the structuredContent — match on the
            # literal, which is stable across both encodings.
            if '"status":"ready"' in text or '"status": "ready"' in text \
                    or '"status":"indexed"' in text:
                ready = True
                break
    except Exception:
        ready = False

    if not ready:
        print(
            f"  [sysctl fact-check] disabled: codebase-memory-mcp found but "
            f"project '{SYSCTL_GRAPH['project']}' is not indexed/ready. "
            f"Sysctl OID names will NOT be verified. To enable, run: "
            f"{SYSCTL_GRAPH['bin']} cli index_repository --repo-path {SRC_ROOT} "
            f"(or set SYSCTL_GRAPH_PROJECT to the correct index name)."
        )
    else:
        print(
            f"  [sysctl fact-check] enabled via codebase-memory-mcp "
            f"(project '{SYSCTL_GRAPH['project']}')."
        )
    _SYSCTL_GRAPH_OK = ready
    return ready


# ---------------------------------------------------------------------------
# 1. Book Text Extraction (PDF / CHM / EPUB)
# ---------------------------------------------------------------------------


def _extract_pdf(filepath: str) -> List[str]:
    """Extract text from PDF — one string per page."""
    pages = []
    try:
        reader = PdfReader(filepath)
        for page in reader.pages:
            txt = page.extract_text()
            if txt:
                pages.append(txt)
    except Exception as e:
        print(f"    warning: could not extract {filepath}: {e}")
    return pages


def _extract_chm(filepath: str) -> List[str]:
    """
    Extract text from CHM via hhextract (hh suite).
    CHM = Microsoft ITSF format — not a standard ZIP.
    Falls back to a warning if hhextract is not available.
    """
    tmp_dir = INDEX_DIR / "chm_tmp"
    tmp_dir.mkdir(exist_ok=True)

    ret = os.system(f"hhextract -o '{tmp_dir}' '{filepath}' 2>/dev/null")
    if ret != 0:
        print(f"    warning: cannot extract CHM {os.path.basename(filepath)}")
        print(f"            install 'hhextract' (hh suite) to support CHM books")
        return []

    pages = []
    for html_file in sorted(tmp_dir.glob("**/*.html"), key=lambda x: x.name):
        try:
            text = re.sub(r"<[^>]+>", " ", html_file.read_text(errors="ignore"))
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 50:
                pages.append(text)
        except Exception:
            continue
    return pages


def _extract_epub(filepath: str) -> List[str]:
    """EPUB is a ZIP of XHTML files — extract text from each."""
    pages = []
    try:
        with zipfile.ZipFile(filepath, "r") as z:
            for name in sorted(z.namelist()):
                if name.endswith((".xhtml", ".html")):
                    content = z.read(name).decode("utf-8", errors="ignore")
                    text = re.sub(r"<[^>]+>", " ", content)
                    text = re.sub(r"\s+", " ", text).strip()
                    if len(text) > 50:
                        pages.append(text)
    except Exception as e:
        print(f"    warning: could not extract EPUB {filepath}: {e}")
    return pages


def build_book_corpus(books_dir: str, force: bool = False) -> str:
    """
    Extract text from all books in books_dir.
    Incremental: skip unchanged files (by hash).
    Returns path to the consolidated corpus file.
    """
    corpus_file = INDEX_DIR / "books_corpus.txt"
    hash_file = INDEX_DIR / "book_hashes.json"
    INDEX_DIR.mkdir(exist_ok=True)

    # Previous hashes
    prev_hashes = {}
    if hash_file.exists():
        prev_hashes = json.loads(hash_file.read_text())

    # Discover books
    book_files = []
    for ext in ("*.pdf", "*.PDF", "*.chm", "*.CHM", "*.epub", "*.EPUB"):
        book_files.extend(glob.glob(os.path.join(books_dir, "**", ext), recursive=True))
    book_files = sorted(set(book_files))

    if not book_files:
        print(f"  warning: no book files found in {books_dir}")
        return str(corpus_file)

    extractors = {
        ".pdf": _extract_pdf,
        ".chm": _extract_chm,
        ".epub": _extract_epub,
    }

    all_entries = []  # (book_name, page_text)
    current_hashes = {}

    for bp in book_files:
        h = hashlib.md5(open(bp, "rb").read()[:8192]).hexdigest()
        current_hashes[bp] = h

        if not force and prev_hashes.get(bp) == h:
            print(f"  skip  {os.path.basename(bp)} (unchanged)")
            continue

        print(f"  extract {os.path.basename(bp)} ...")
        ext = os.path.splitext(bp)[1].lower()
        extractor = extractors.get(ext)
        if extractor:
            pages = extractor(bp)
            all_entries.extend((os.path.basename(bp), p) for p in pages)
            print(f"         {len(pages)} pages")

    # Detect books that were present in a previous run but are gone now.
    # Without this, their text + hash entry would linger in the corpus and
    # in book_hashes.json forever, polluting search results with content
    # the user thought they had removed.
    deleted_paths = set(prev_hashes.keys()) - set(current_hashes.keys())
    deleted_basenames = {os.path.basename(p) for p in deleted_paths}
    for d in sorted(deleted_basenames):
        print(f"  prune {d} (no longer in {books_dir})")

    # Write corpus — preserve existing FreeBSD docs (appended by extract_freebsd_docs)
    # On incremental runs, all_entries only has changed books, so we need to keep
    # unchanged book content + FreeBSD docs from the previous corpus.
    previous_corpus = ""
    if not force and corpus_file.exists():
        previous_corpus = corpus_file.read_text(encoding="utf-8", errors="ignore")

    # Unfiltered snapshot of what is on disk right now, used only to decide
    # whether the final write is a no-op. Kept separate from previous_corpus,
    # which is "" under --force and is further filtered by drop_sources below.
    corpus_on_disk = (corpus_file.read_text(encoding="utf-8", errors="ignore")
                      if corpus_file.exists() else None)

    # Sources we don't want carried over from the previous corpus:
    #   - books being re-extracted (will be re-appended below with fresh text)
    #   - books that have been deleted from books_dir (should disappear entirely)
    re_extracted = {os.path.basename(bp) for bp in book_files
                    if prev_hashes.get(bp) != current_hashes.get(bp)
                    or (force and bp in current_hashes)}
    drop_sources = re_extracted | deleted_basenames

    # Build the new corpus text in memory so the on-disk write is atomic —
    # a Ctrl-C during the write would otherwise leave a truncated corpus and
    # the next run would silently search against partial content.
    new_corpus_parts = []
    if previous_corpus:
        if drop_sources:
            # Keep only segments whose source is NOT being dropped.
            segments = re.split(r"### SOURCE: (.+?) ###", previous_corpus)
            # segments: [before, source1, text1, source2, text2, ...]
            kept = segments[0:1]  # leading text before first source
            for i in range(1, len(segments), 2):
                src = segments[i].strip()
                if src not in drop_sources:
                    kept.append(segments[i])
                    kept.append(segments[i + 1])
            previous_corpus = "".join(kept) if kept else ""
        new_corpus_parts.append(previous_corpus)
    for book_name, page_text in all_entries:
        new_corpus_parts.append(f"\n\n### SOURCE: {book_name} ###\n\n")
        new_corpus_parts.append(page_text)
    # Only write when the bytes changed. _atomic_write renames into place,
    # which always produces a fresh mtime — and get_or_build_index treats a
    # corpus newer than the index as stale, so an unconditional write here
    # forced a full ~100MB TF-IDF rebuild on every single run. It also made
    # two concurrent runners (one per LLM endpoint) race on the same .npy.
    new_corpus = "".join(new_corpus_parts)
    if new_corpus != corpus_on_disk:
        _atomic_write(str(corpus_file), new_corpus)

    # Atomic write — a Ctrl-C mid-write would otherwise leave a truncated JSON
    # that crashes the next run before it can rebuild.
    _atomic_write(str(hash_file), json.dumps(current_hashes, indent=2))
    print(f"\n  corpus: {len(all_entries)} pages from {len(current_hashes)} books\n")
    return str(corpus_file)


def _clean_troff(text: str) -> str:
    """
    Convert troff/groff man page source to readable plain text.

    Handles indentation (.in), paragraph macros (.PP, .LP, .P),
    section headings (.SH), function names (.Fn), file paths (.Pa),
    emphasis (.Em), bold (.Bf/.Ef), and removes control requests.

    Pure Python — no unfmt/mandoc dependency needed.
    """
    lines = text.split("\n")
    cleaned = []
    indent = 0

    for line in lines:
        # Handle indentation
        if line.startswith(".in +"):
            indent += 1
            continue
        elif line.startswith(".in -"):
            indent = max(0, indent - 1)
            continue
        elif line.startswith(".in") and "\\&" not in line:
            # Reset indent on bare .in
            m = re.match(r"\.in\s*(\d*)", line)
            if m:
                indent = int(m.group(1) or 0)
            continue

        # Section headings
        m = re.match(r"\.SH\s+(.+)", line)
        if m:
            cleaned.append("\n" + m.group(1).strip().upper() + "\n" + "=" * len(m.group(1)))
            continue

        # Sub-section headings
        m = re.match(r"\.SS\s+(.+)", line)
        if m:
            cleaned.append("\n" + m.group(1).strip() + "\n" + "-" * len(m.group(1)))
            continue

        # Paragraph macros — skip, just add blank line
        if line.strip() in (".PP", ".LP", ".P", ".np", ".LP0"):
            if cleaned and cleaned[-1]:
                cleaned.append("")
            continue

        # Block formatting
        if line.strip().startswith(".Bf"):
            continue
        if line.strip() in (".Ef", ".Fl", ".Bl -bullet", ".Bl -dash",
                            ".Bl -item", ".Bl -enum", ".El", ".It"):
            continue

        # Remove other troff requests (lines starting with .)
        if re.match(r"^\.\w+", line) and not re.match(r"^\.\s*$", line):
            continue

        # Clean inline macros
        t = line
        t = re.sub(r"\\f[BIPR]", "", t)       # font changes
        t = re.sub(r"\\f\^", "", t)           # font reset
        t = re.sub(r"\\[fFnPaIcE]*\[?", "", t)  # remaining macro noise

        # Expand common inline macros
        t = re.sub(r"\.Em\s+(.+)", r"\1", t)     # emphasis
        t = re.sub(r"\.Fa\s+(.+)", r"\1", t)     # function arg
        t = re.sub(r"\.Fn\s+(.+)", r"\1", t)     # function name
        t = re.sub(r"\.Pa\s+(.+)", r"\1", t)     # path
        t = re.sub(r"\.Sy\s+(.+)", r"\1", t)     # synonym
        t = re.sub(r"\.Dv\s+(.+)", r"\1", t)     # device/value
        t = re.sub(r"\.Va\s+(.+)", r"\1", t)     # variable
        t = re.sub(r"\.Fl\s+(\S+)", r"\1", t)    # flag

        # Remove special characters
        t = t.replace("\\&", "")    # escaped space
        t = t.replace("\\~", "~")   # tilde
        t = t.replace("\\-", "-")   # hyphen
        t = t.replace("\\(em", "\"")  # em dash
        t = t.replace("\\(en", "'")   # en dash
        t = t.replace("\\(aq", "'")   # apostrophe

        # Apply indentation
        if t.strip():
            cleaned.append("    " * indent + t.strip())
        else:
            cleaned.append("")

    # Collapse excessive blank lines
    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _clean_asciidoc(text: str) -> str:
    """
    Convert AsciiDoc source to readable plain text.

    Strips AsciiDoc markup: includes, conditional blocks, attributes,
    role markers, section headers, and list markers. Keeps the prose.
    """
    lines = text.split("\n")
    cleaned = []

    for line in lines:
        # Skip include directives
        if line.strip().startswith("include::"):
            continue
        # Skip conditional blocks
        if line.strip().startswith("ifdef::") or line.strip().startswith("ifndef::"):
            continue
        if line.strip() in ("endif::[]", "endif::"):
            continue
        # Skip attribute definitions
        if re.match(r"^[=:]\w+", line) or line.strip().startswith(":"):
            continue
        # Skip YAML frontmatter delimiters
        if line.strip() == "---":
            continue
        # Skip role/anchor markers
        if line.strip().startswith("[[") or line.strip().startswith("[["):
            continue

        t = line
        # Remove AsciiDoc section headers (= Header)
        t = re.sub(r"^={1,6}\s+", "", t)
        # Remove bold/italic markup
        t = re.sub(r"[*_`]{1,3}([^*_`]+)[*_`]{1,3}", r"\1", t)
        # Remove link syntax [[anchor]], link:url[text], https:url[]
        t = re.sub(r"\[\[.*?\]\]", "", t)
        t = re.sub(r"link:\S+\[([^\]]*)\]", r"\1", t)
        t = re.sub(r"https?://\S+\[\]", "", t)
        # Remove role markers
        t = re.sub(r"\[^.*?\]", "", t)
        # Remove list markers
        t = re.sub(r"^(?:[*\-•]|\d+\.)\s+", "", t)
        # Remove inline source code backticks
        t = t.replace("`, ", " ").replace(", `", " ").replace("`", "")

        if t.strip():
            cleaned.append(t.strip())
        else:
            cleaned.append("")

    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _extract_git_log(src_root: str, path: str, max_commits: int = 15) -> str:
    """
    Extract meaningful commit messages for a source file from git log.

    Returns commit subject + body for the most recent N commits touching
    this file. This gives the agent context on WHY code was written a
    certain way — design decisions, bug fixes, refactoring rationale.

    Uses `git log --follow` to track file renames.
    """
    full = os.path.join(src_root, path)
    try:
        result = subprocess.run(
            ["git", "-C", src_root, "log", "--follow",
             f"--format=%h%n%s%n%b%n---COMMIT_SEP---",
             "-n", str(max_commits), "--", full],
            capture_output=True, text=True, errors='replace', timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""

        commits = result.stdout.split("---COMMIT_SEP---")
        entries = []
        for commit in commits:
            commit = commit.strip()
            if not commit:
                continue
            lines = commit.split("\n")
            hash_ = lines[0] if lines else ""
            subject = lines[1] if len(lines) > 1 else ""
            body = "\n".join(lines[2:]) if len(lines) > 2 else ""

            # Skip merge commits and trivial messages
            if subject.startswith("Merge ") or len(subject) < 10:
                continue

            entry = f"  {hash_}: {subject}"
            if body.strip():
                entry += f"\n  {body.strip()[:300]}"
            entries.append(entry)

        return "\n\n".join(entries[:max_commits])
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return ""


def extract_freebsd_docs(src_root: str, corpus_file: str) -> int:
    """
    Extract FreeBSD man pages (man9), technical papers, kernel docs,
    freebsd-doc handbook articles, and git commit log context
    into the book corpus. Appends to the existing corpus file.

    Sources:
      - share/man/man9/* — kernel API man pages (troff format)
      - share/doc/papers/* — technical papers
      - sys/README.md — kernel source roadmap
      - tools/kerneldoc/ — Doxygen subsystem descriptions
      - $HOME/freebsd-doc/documentation/content/en/ — Handbook, FAQ, articles (AsciiDoc)
      - git log of key source files — developer commit messages (design rationale)

    Returns the number of documents extracted.
    """
    all_entries = []  # (source_label, text)

    # --- man9 kernel API man pages ---
    man9_dir = os.path.join(src_root, "share", "man", "man9")
    if os.path.isdir(man9_dir):
        for fname in sorted(os.listdir(man9_dir)):
            if not fname.endswith(".man9") and not fname.endswith(".9"):
                continue
            fpath = os.path.join(man9_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                raw = Path(fpath).read_text(errors="ignore")
                text = _clean_troff(raw)
                if len(text) > 200:  # skip empty/broken pages
                    label = f"FreeBSD man9: {fname}"
                    all_entries.append((label, text))
            except Exception:
                continue

    # --- technical papers ---
    papers_dir = os.path.join(src_root, "share", "doc", "papers")
    if os.path.isdir(papers_dir):
        for fname in sorted(os.listdir(papers_dir)):
            fpath = os.path.join(papers_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                if fname.endswith((".txt", ".md", ".asc")):
                    text = Path(fpath).read_text(errors="ignore")
                elif fname.endswith((".gz", ".bz2")):
                    # Skip compressed — would need decompression
                    continue
                else:
                    # Try as plain text
                    text = Path(fpath).read_text(errors="ignore")
                if len(text) > 200:
                    label = f"FreeBSD paper: {fname}"
                    all_entries.append((label, text))
            except Exception:
                continue

    # --- sys/README.md (kernel roadmap) ---
    sys_readme = os.path.join(src_root, "sys", "README.md")
    if os.path.isfile(sys_readme):
        try:
            text = Path(sys_readme).read_text(errors="ignore")
            if len(text) > 100:
                all_entries.append(("FreeBSD sys/README.md", text))
        except Exception:
            pass

    # --- tools/kerneldoc Doxyfiles (subsystem descriptions) ---
    kerneldoc_dir = os.path.join(src_root, "tools", "kerneldoc")
    if os.path.isdir(kerneldoc_dir):
        for fname in sorted(os.listdir(kerneldoc_dir)):
            if not fname.startswith("Doxyfile-"):
                continue
            fpath = os.path.join(kerneldoc_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                text = Path(fpath).read_text(errors="ignore")
                if len(text) > 50:
                    label = f"FreeBSD kerneldoc: {fname}"
                    all_entries.append((label, text[:3000]))  # limit size
            except Exception:
                continue

    # --- freebsd-doc handbook/articles (AsciiDoc, English only) ---
    freebsd_doc_dir = FREEBSD_DOC
    if os.path.isdir(freebsd_doc_dir):
        for root, _dirs, files in os.walk(freebsd_doc_dir):
            for fname in sorted(files):
                if not fname.endswith(".adoc"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    raw = Path(fpath).read_text(errors="ignore")
                    text = _clean_asciidoc(raw)
                    if len(text) > 200:
                        rel = os.path.relpath(fpath, freebsd_doc_dir)
                        label = f"FreeBSD Handbook: {rel}"
                        all_entries.append((label, text))
                except Exception:
                    continue

    # --- git commit log for key kernel subsystems (design rationale) ---
    key_paths_for_log = [
        "sys/kern/init_main.c",
        "sys/vm/vm_page.c",
        "sys/vm/vm_map.c",
        "sys/vm/vm_object.c",
        "sys/kern/kern_fork.c",
        "sys/kern/kern_exit.c",
        "sys/kern/kern_jail.c",
        "sys/kern/vfs_mount.c",
        "sys/kern/vfs_lookup.c",
        "sys/kern/uipc_socket.c",
        "sys/net/netisr.c",
        "sys/net/if.c",
        "sys/netinet/ip_input.c",
        "sys/ufs/ffs/ffs_softdep.c",
        "sys/kern/subr_bus.c",
        "sys/kern/kern_intr.c",
        "sys/kern/vfs_bio.c",
    ]
    for kp in key_paths_for_log:
        full = os.path.join(src_root, kp)
        if os.path.isfile(full):
            log_text = _extract_git_log(src_root, kp)
            if log_text:
                label = f"FreeBSD git log: {kp}"
                all_entries.append((label, log_text))

    # Write to corpus — first strip any existing FreeBSD docs entries to avoid duplicates
    if os.path.exists(corpus_file):
        old_content = Path(corpus_file).read_text(encoding="utf-8", errors="ignore")
        # Remove existing FreeBSD-sourced entries (man9, paper, Handbook, git log, kerneldoc, sys/README)
        segments = re.split(r"### SOURCE: (.+?) ###", old_content)
        kept = [segments[0].rstrip("\n")] if segments else []
        freebsd_prefixes = ("FreeBSD man9:", "FreeBSD paper:", "FreeBSD Handbook:",
                            "FreeBSD git log:", "FreeBSD kerneldoc:", "FreeBSD sys/")
        for i in range(1, len(segments), 2):
            src = segments[i].strip()
            if not any(src.startswith(p) for p in freebsd_prefixes):
                # Re-emit the delimiter, not just the label. re.split() strips
                # the "### SOURCE: X ###" markers out, so reassembling from the
                # bare capture groups silently destroyed the header of every
                # retained (book) segment — after one cycle the whole book
                # corpus was a single unlabeled blob, un-prunable by
                # build_book_corpus's drop_sources and unattributable by
                # search_books.
                body = segments[i + 1] if i + 1 < len(segments) else ""
                kept.append(f"\n\n### SOURCE: {src} ###\n\n{body.strip(chr(10))}")
        old_content = "".join(kept) if kept else ""

        # Build the new corpus in memory and only write when the bytes
        # actually change. Each segment is normalized to exactly one blank
        # line before its header; previously the strip left the old
        # separator in place and the re-append added another, growing the
        # corpus by 2 bytes per run forever (observed at 100 stacked
        # newlines). That churn changed the file every run, so
        # get_or_build_index always saw "corpus newer than index" and
        # rebuilt the ~100MB TF-IDF matrix — and two concurrent runners
        # (one per LLM endpoint) would race to write the same .npy.
        new_content = old_content + "".join(
            f"\n\n### SOURCE: {label} ###\n\n{text.strip(chr(10))}"
            for label, text in all_entries
        )
        if new_content != Path(corpus_file).read_text(encoding="utf-8",
                                                      errors="ignore"):
            Path(corpus_file).write_text(new_content, encoding="utf-8")
    else:
        with open(corpus_file, "a", encoding="utf-8") as f:
            for label, text in all_entries:
                f.write(f"\n\n### SOURCE: {label} ###\n\n")
                f.write(text)

    # Count by category
    man_count = sum(1 for l, _ in all_entries if "man9" in l)
    paper_count = sum(1 for l, _ in all_entries if "paper" in l)
    handbook_count = sum(1 for l, _ in all_entries if "Handbook" in l)
    git_count = sum(1 for l, _ in all_entries if "git log" in l)
    other_count = sum(
        1 for l, _ in all_entries
        if "man9" not in l and "paper" not in l
        and "Handbook" not in l and "git log" not in l
    )
    parts = []
    if man_count:
        parts.append(f"{man_count} man9 pages")
    if paper_count:
        parts.append(f"{paper_count} papers")
    if handbook_count:
        parts.append(f"{handbook_count} handbook articles")
    if git_count:
        parts.append(f"{git_count} git log contexts")
    if other_count:
        parts.append(f"{other_count} other docs")

    if parts:
        print(f"  FreeBSD docs: {', '.join(parts)}")
    else:
        print(f"  FreeBSD docs: none found (src tree may be incomplete)")

    return len(all_entries)


# ---------------------------------------------------------------------------
# 2. TF-IDF Semantic Search (numpy only — no heavy ML deps)
# ---------------------------------------------------------------------------


class TfidfIndex:
    """
    Lightweight TF-IDF index over text chunks.
    Uses numpy for vector math — no scikit-learn or PyTorch needed.
    """

    CHUNK_SIZE = 1200
    CHUNK_OVERLAP = 200

    def __init__(self):
        self.chunks = []        # list of (source_label, text)
        self.vocab = {}         # term → col_index
        self.tf_idf_matrix = None  # numpy 2darray, rows=chunks
        self.sources = []       # parallel list of source labels

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        return re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower())

    @staticmethod
    def _split_by_paragraphs(text: str) -> List[str]:
        """Split text into paragraphs (separated by blank lines)."""
        paragraphs = re.split(r'\n\s*\n', text)
        return [p.strip() for p in paragraphs if len(p.strip()) > 30]

    @staticmethod
    def _split_by_asciidoc_sections(text: str) -> List[str]:
        """Split AsciiDoc text by level-1/level-2 headers (== Title)."""
        parts = re.split(r'\n==+\s+', text)
        return [p.strip() for p in parts if len(p.strip()) > 100]

    @staticmethod
    def _split_by_git_commits(text: str) -> List[str]:
        """Split git log text by commit entries (hash on its own line)."""
        parts = re.split(r'\n(?=[0-9a-f]{7,})', text)
        return [p.strip() for p in parts if len(p.strip()) > 50]

    @staticmethod
    def _merge_paragraphs(paragraphs: List[str], max_size: int) -> List[str]:
        """Merge paragraphs into chunks up to max_size chars, keeping boundaries."""
        if not paragraphs:
            return []
        chunks = []
        current = paragraphs[0]
        for para in paragraphs[1:]:
            # Try adding the next paragraph
            candidate = current + "\n\n" + para
            if len(candidate) <= max_size:
                current = candidate
            else:
                chunks.append(current)
                current = para
        chunks.append(current)
        return chunks

    def _chunk_text(self, text: str, source: str):
        """Split text into overlapping chunks by semantic boundaries."""
        # Detect source type from source label
        is_asciidoc = "freebsd-doc" in source.lower() or "handbook" in source.lower()
        is_git_log = source.startswith("FreeBSD git log")
        is_manpage = source.startswith("FreeBSD man9")

        paragraphs = []
        if is_asciidoc:
            paragraphs = self._split_by_asciidoc_sections(text)
        elif is_git_log:
            paragraphs = self._split_by_git_commits(text)
        else:
            paragraphs = self._split_by_paragraphs(text)

        if not paragraphs:
            # Fallback to fixed-size chunking
            self._chunk_text_fixed(text, source)
            return

        # Merge paragraphs into chunks respecting boundaries
        merged = self._merge_paragraphs(paragraphs, self.CHUNK_SIZE)

        # Add overlap between chunks
        for i, chunk in enumerate(merged):
            if len(chunk) < 100:
                continue
            # Add tail overlap from next chunk
            if i + 1 < len(merged):
                next_chunk = merged[i + 1]
                overlap_text = "\n\n" + next_chunk[:self.CHUNK_OVERLAP]
                chunk = chunk + overlap_text
            self.chunks.append((source, chunk))

    def _chunk_text_fixed(self, text: str, source: str):
        """Fallback: split text into overlapping fixed-size chunks."""
        for i in range(0, max(0, len(text) - self.CHUNK_OVERLAP),
                       self.CHUNK_SIZE - self.CHUNK_OVERLAP):
            chunk = text[i : i + self.CHUNK_SIZE]
            if len(chunk) > 200:  # skip tiny fragments
                self.chunks.append((source, chunk))

    def build(self, corpus_path: str):
        """Build index from corpus file."""
        print("  tokenizing corpus ...")
        content = Path(corpus_path).read_text(encoding="utf-8", errors="ignore")

        # Split by source markers
        segments = re.split(r"### SOURCE: (.+?) ###", content)
        # segments: [before, source1, text1, source2, text2, ...]
        for i in range(1, len(segments), 2):
            self._chunk_text(segments[i + 1], segments[i].strip())

        if not self.chunks:
            print("  warning: no chunks extracted from corpus")
            return

        # Build vocabulary
        doc_freq: Dict[str, int] = {}
        chunk_terms: List[set] = []

        for _, chunk in self.chunks:
            terms = set(self._tokenize(chunk))
            chunk_terms.append(terms)
            for t in terms:
                doc_freq[t] = doc_freq.get(t, 0) + 1

        # Filter: keep terms that appear in 2..0.5*N documents
        n = len(self.chunks)
        self.vocab = {
            t: idx for idx, (t, df) in enumerate(
                [(k, v) for k, v in sorted(doc_freq.items()) if 2 <= v <= n * 0.5],
            )
        }
        print(f"  vocabulary: {len(self.vocab)} terms, {len(self.chunks)} chunks")

        # Build TF-IDF matrix
        matrix = np.zeros((n, len(self.vocab)), dtype=np.float32)
        for row, terms in enumerate(chunk_terms):
            tf: Dict[str, int] = {}
            for t in self._tokenize(self.chunks[row][1]):
                if t in self.vocab:
                    tf[t] = tf.get(t, 0) + 1
            for t, count in tf.items():
                col = self.vocab[t]
                idf = math.log(n / (1 + doc_freq[t])) + 1
                matrix[row, col] = count * idf

        # L2 normalize rows
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        self.tf_idf_matrix = matrix / norms
        self.sources = [s for s, _ in self.chunks]

    def search(self, query: str, top_k: int = 4) -> List[Tuple[str, str]]:
        """Return list of (source, chunk_text) most similar to query."""
        if self.tf_idf_matrix is None or not self.vocab:
            return []

        qvec = np.zeros(len(self.vocab), dtype=np.float32)
        for t in self._tokenize(query):
            if t in self.vocab:
                qvec[self.vocab[t]] += 1

        norm = np.linalg.norm(qvec)
        if norm == 0:
            return []
        qvec /= norm

        scores = self.tf_idf_matrix @ qvec
        top_idx = np.argsort(-scores)[:top_k]

        results = []
        for idx in top_idx:
            if scores[idx] > 0:
                results.append((self.sources[idx], self.chunks[idx][1]))
        return results

    def save(self, path: str):
        """Atomically persist the index.

        np.save and the meta JSON go to temp paths first, then both get
        renamed into place. If anything fails partway, leftover temp files
        are removed and the previous on-disk index (if any) stays intact.

        This matters because a corrupted matrix.npy or truncated meta.json
        would silently produce empty book-search results on the next run —
        chapters would be generated with no book grounding and the only
        symptom would be visibly worse output.
        """
        path = str(path)
        matrix_path = f"{path}_matrix.npy"
        meta_path = f"{path}_meta.json"
        matrix_tmp = f"{matrix_path}.tmp.{os.getpid()}"
        meta_tmp = f"{meta_path}.tmp.{os.getpid()}"

        # np.save auto-appends ".npy" if the path doesn't already end in it,
        # so we write to a path that already does — otherwise os.replace below
        # would look for the wrong filename and fail.
        matrix_tmp_actual = matrix_tmp + ".npy"

        try:
            # np.save needs a real file path — write to a temp, then rename.
            np.save(matrix_tmp, self.tf_idf_matrix)
            # _atomic_write handles the meta JSON (fsync + os.replace).
            _atomic_write(meta_path, json.dumps({
                "vocab": self.vocab,
                "chunks": self.chunks,
                "sources": self.sources,
            }))
            # If meta wrote successfully, swap in the matrix.
            os.replace(matrix_tmp_actual, matrix_path)
        except Exception:
            for tmp in (matrix_tmp, matrix_tmp_actual, meta_tmp):
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            raise

    def load(self, path: str) -> bool:
        """Load a saved index and validate it. Returns True on success.

        Returns False (instead of raising or returning a half-loaded index)
        when:
          - either file is missing
          - the .npy is unreadable / truncated
          - the JSON is malformed
          - the matrix shape is inconsistent with the meta
            (vocab size != n_columns, n_rows != len(chunks) != len(sources))

        The caller should treat False as "rebuild from corpus" rather than
        carrying on with empty searches.
        """
        path = str(path)
        matrix_path = f"{path}_matrix.npy"
        meta_path = f"{path}_meta.json"

        if not (os.path.exists(matrix_path) and os.path.exists(meta_path)):
            return False

        try:
            matrix = np.load(matrix_path)
        except Exception as e:
            print(f"  ⚠ index matrix unreadable ({e}) — will rebuild")
            return False

        try:
            meta = json.loads(Path(meta_path).read_text())
        except Exception as e:
            print(f"  ⚠ index meta unreadable ({e}) — will rebuild")
            return False

        vocab = meta.get("vocab")
        chunks = meta.get("chunks")
        sources = meta.get("sources")

        if not isinstance(vocab, dict) or not isinstance(chunks, list) \
                or not isinstance(sources, list):
            print("  ⚠ index meta has unexpected types — will rebuild")
            return False

        if matrix.ndim != 2:
            print(f"  ⚠ index matrix has wrong rank {matrix.ndim} — will rebuild")
            return False

        n_rows, n_cols = matrix.shape
        if n_rows != len(chunks) or n_rows != len(sources) or n_cols != len(vocab):
            print(
                f"  ⚠ index shape mismatch "
                f"(matrix={n_rows}x{n_cols}, "
                f"chunks={len(chunks)}, sources={len(sources)}, vocab={len(vocab)}) "
                f"— will rebuild"
            )
            return False

        self.tf_idf_matrix = matrix
        self.vocab = vocab
        self.chunks = chunks
        self.sources = sources
        return True


def get_or_build_index(corpus_path: str, force: bool = False) -> TfidfIndex:
    """Load saved index or build from corpus.

    Rebuilds when:
      - `force` is True (caller asked for a clean rebuild), or
      - the matrix .npy is missing, or
      - the corpus on disk is newer than the saved matrix (incremental
        runs that added/removed books would otherwise reuse a stale
        index that doesn't reflect the new corpus), or
      - the saved index fails any validation check (corrupt .npy,
        malformed meta, shape mismatch).
    """
    index_path = INDEX_DIR / "tfidf_index"
    matrix_file = Path(f"{index_path}_matrix.npy")

    if not force and matrix_file.exists():
        # If the corpus is newer than the saved index, the index is
        # stale relative to disk state. Force a rebuild rather than
        # silently search against the old chunks.
        try:
            corpus_mtime = os.path.getmtime(corpus_path)
            index_mtime = matrix_file.stat().st_mtime
        except OSError:
            corpus_mtime = index_mtime = 0
        if corpus_mtime > index_mtime:
            print("  corpus is newer than saved index — rebuilding")
        else:
            print("  loading saved TF-IDF index ...")
            idx = TfidfIndex()
            if idx.load(str(index_path)):
                print(f"  index: {len(idx.chunks)} chunks, {len(idx.vocab)} terms")
                return idx
            print("  saved index could not be validated — rebuilding from corpus")

    print("  building TF-IDF index ...")
    idx = TfidfIndex()
    idx.build(corpus_path)
    idx.save(str(index_path))
    print("  index saved.\n")
    return idx


# ---------------------------------------------------------------------------
# 3. smolagent Tools
# ---------------------------------------------------------------------------


def _safe_src_path(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve an agent-supplied `path` against SRC_ROOT and confine it.

    The writer/reviewer agents choose these paths, so a naive
    `os.path.join(SRC_ROOT, path)` is a traversal hole: `../../etc/passwd`
    climbs out of the tree, and an absolute `path` makes `os.path.join`
    discard SRC_ROOT entirely. Both let the agent read any file the
    process can.

    Returns `(resolved_abspath, None)` when `path` stays inside SRC_ROOT,
    or `(None, error_message)` when it escapes. Callers surface the error
    string in their own tool-output style. Symlinks are resolved with
    `realpath` on both ends so a symlink inside the tree can't be used to
    hop out either.
    """
    root = os.path.realpath(SRC_ROOT)
    full = os.path.realpath(os.path.join(SRC_ROOT, path))
    if full == root or full.startswith(root + os.sep):
        return full, None
    return None, f"Error: '{path}' resolves outside the source tree"


class ReadFreeBSDSource(Tool):
    """Read source code files from the FreeBSD tree."""

    name = "read_freebsd_source"
    description = (
        "Read a source file from the FreeBSD source tree. "
        "Returns up to 4000 chars. Use relative paths like "
        "'sys/kern/init_main.c' or 'sys/vm/vm_page.c'."
    )
    inputs = {
        "path": {
            "type": "string",
            "description": "Path relative to FreeBSD src root",
        }
    }
    output_type = "string"

    def forward(self, path: str) -> str:
        full, err = _safe_src_path(path)
        if err:
            return err
        try:
            if not os.path.exists(full):
                # Try glob for partial matches
                candidates = glob.glob(full.replace("*", "*"))
                if candidates:
                    return f"File not found at exact path, similar files:\n" + "\n".join(
                        os.path.relpath(c, SRC_ROOT) for c in candidates[:10]
                    )
                return f"Error: '{path}' not found in {SRC_ROOT}"
            content = Path(full).read_text(errors="ignore")
            if len(content) > 4000:
                content = content[:4000] + "\n... (truncated)"
            return f"--- {path} ---\n" + content
        except Exception as e:
            return f"Error reading {path}: {e}"


class SearchBooks(Tool):
    """Semantic search over the FreeBSD book corpus using TF-IDF."""

    name = "search_books"
    description = (
        "Search the FreeBSD documentation corpus for concepts, architecture "
        "descriptions, and historical context. The corpus includes:\n"
        "  - FreeBSD books (PDFs): McKusick, Device Drivers, etc.\n"
        "  - FreeBSD man9 kernel API man pages\n"
        "  - FreeBSD Handbook and articles (AsciiDoc)\n"
        "  - Technical papers from the source tree\n"
        "  - Git commit messages (design rationale from developers)\n"
        "Returns the most relevant excerpts with source attribution.\n"
        "Good queries: 'virtual memory architecture', 'buffer cache',\n"
        "'soft updates', 'jail security model', 'netisr design'."
    )
    inputs = {
        "query": {
            "type": "string",
            "description": "Concept or keyword to search for",
        }
    }
    output_type = "string"

    def __init__(self, index: TfidfIndex):
        super().__init__()
        self.index = index

    def forward(self, query: str) -> str:
        results = self.index.search(query, top_k=4)
        if not results:
            return (
                f"No book excerpts found for '{query}'. "
                "Try broader terms or check the spelling."
            )
        lines = [f"=== Book search results for: {query} ===\n"]
        for source, chunk in results:
            lines.append(f"[Source: {source}]")
            lines.append(chunk[:600])
            lines.append("")
        return "\n".join(lines)


class ExploreTree(Tool):
    """List directory contents in the FreeBSD source tree."""

    name = "explore_tree"
    description = (
        "List files and directories in the FreeBSD source tree. "
        "Use this to discover what files exist before reading them. "
        "Returns up to 80 entries. Example: 'sys/vm' or 'stand/efi/loader'."
    )
    inputs = {
        "path": {
            "type": "string",
            "description": "Path relative to FreeBSD src root",
        }
    }
    output_type = "string"

    def forward(self, path: str) -> str:
        full, err = _safe_src_path(path)
        if err:
            return err
        try:
            if not os.path.isdir(full):
                return f"Not a directory: '{path}'"
            entries = []
            for e in sorted(os.listdir(full)):
                fp = os.path.join(full, e)
                kind = "📁" if os.path.isdir(fp) else "📄"
                entries.append(f"  {kind} {e}")
            entries = entries[:80]
            return f"--- {path} ---\n" + "\n".join(entries)
        except Exception as e:
            return f"Error listing {path}: {e}"


# ---------------------------------------------------------------------------
# 3c2. Directory Map Tool (structured one-level summary of a directory)
# ---------------------------------------------------------------------------

# Per-tool budget. The writer agent has a max_steps budget and a context
# window; a single directory_map call must fit comfortably in both.
_DIRMAP_OUTPUT_CAP = 6000
_DIRMAP_FILE_LIMIT = 60     # max .c/.h files summarised per call
_DIRMAP_FILE_SIZE_CAP = 256 * 1024   # skip giant generated files
_DIRMAP_NAMES_PER_FILE = 6  # max symbols listed per file
_DIRMAP_HEADER_COMMENT_CAP = 240


def _file_header_comment(text: str) -> str:
    """Extract the first /* ... */ comment near the top of a C file.

    Many FreeBSD source files have a one-paragraph "what this file does"
    comment right after the BSD license block. We:
      1. Cut the search window at the first non-comment, non-include
         token (`#define`, `struct`, `typedef`, function decl, etc.)
         so trailing comments like `/* !_FOO_H_ */` after a
         `#endif` don't get treated as the file purpose.
      2. Skip the license block (comments that contain 'Copyright' or
         'Redistribution and use').
      3. Skip header-guard echo comments (`/* _FOO_H_ */`,
         `/* !_FOO_H_ */`) that some header authors place on the
         closing `#endif`.

    Returns "" when no usable comment is found.
    """
    # Bound the window: stop at the first line that looks like the
    # start of code (a #define, a struct/typedef/enum, or a likely
    # function declaration). Until that line, we are still in the
    # header preamble where a purpose comment would live.
    code_start = re.search(
        r'(?m)^\s*(?:#define\s|struct\s+\w+\s*\{|typedef\s|enum\s+\w*\s*\{'
        r'|extern\s|static\s|void\s|int\s|char\s|uint\w*\s|u_int\w*\s)',
        text,
    )
    end = code_start.start() if code_start else 4000
    # Also cap at 6 KB regardless of where code starts, in case the
    # file has no obvious code marker in the first 6 KB.
    end = min(end, 6000)
    head = text[:end]

    matches = list(re.finditer(r'/\*(.*?)\*/', head, re.DOTALL))
    for m in matches:
        body = m.group(1)
        if 'Copyright' in body or 'Redistribution and use' in body:
            continue
        # Strip leading-asterisk noise that pretty-prints multi-line
        # block comments.
        cleaned = re.sub(r'(?m)^\s*\*\s?', '', body).strip()
        if not cleaned:
            continue
        # Skip header-guard echo comments: a single token like
        # `_FOO_H_`, `!_FOO_H_`, or `__FOO_H__`. These are conventional
        # closing markers and never describe what a file does.
        if re.fullmatch(r'!?_+\w+_+', cleaned):
            continue
        if len(cleaned) > _DIRMAP_HEADER_COMMENT_CAP:
            cleaned = cleaned[:_DIRMAP_HEADER_COMMENT_CAP].rstrip() + '…'
        # Collapse internal whitespace so the line is compact in the
        # tool output.
        cleaned = re.sub(r'\s+', ' ', cleaned)
        return cleaned
    return ""


def _dirmap_extract_names(content: str) -> Tuple[List[str], List[str]]:
    """Return (struct_names, func_names) extracted from one C/H file.

    Reuses the same regex strategy as _extract_struct_defs and
    _extract_func_sigs, but returns just names (no field lists or file
    annotations) so the directory map stays terse.
    """
    structs = []
    seen_structs = set()
    for m in re.finditer(
        r'^\s*struct\s+(\w+)\s*\{', content, re.MULTILINE
    ):
        name = m.group(1)
        if name not in seen_structs:
            seen_structs.add(name)
            structs.append(name)

    funcs = []
    seen_funcs = set()
    skip = {'if', 'else', 'while', 'for', 'switch', 'return',
            'struct', 'union', 'enum', 'typedef', 'define',
            'sizeof', 'do'}
    # Match function definitions starting at column 0. The previous
    # single-line regex required the return type and name on the same
    # line, which missed FreeBSD's K&R-ish style:
    #     static enum mlx5_dev_event
    #     dcbx_subevent(u8 subtype)
    #     {
    # plus multi-line arg lists like:
    #     bool
    #     in_pcbrele(struct inpcb *inp,
    #                struct inpcbinfo *pcbinfo)
    #     {
    # The new regex consumes one or more identifier-token chunks
    # (covering "static", "enum mlx5_dev_event", "struct mlx5_eqe *",
    # qualifiers, etc.), then captures the function name, then an arg
    # list that may span lines (no { or ; inside), then optional GCC
    # attributes, then the opening {. Side benefit: the OLD regex
    # falsely matched control-flow keywords like `if`/`for`/`switch`/
    # `TAILQ_FOREACH` because their `(...)` shape looked like a defn;
    # NEW excludes them by structure (the chunk before the name must
    # itself be an identifier-token, which `if (` doesn't satisfy).
    func_def_re = re.compile(
        r"^"
        r"(?:[A-Za-z_]\w*\s*\**\s+)+"      # return type + qualifiers
        r"\*?\s*([A-Za-z_]\w*)\s*"          # function name
        r"\([^{;]*?\)\s*"                   # arg list (may span lines)
        r"(?:__\w+(?:\s*\([^)]*\))?\s*)*"   # optional GCC attributes
        r"\{",
        re.MULTILINE | re.DOTALL,
    )
    for m in func_def_re.finditer(content):
        name = m.group(1)
        if name in skip or name in seen_funcs:
            continue
        seen_funcs.add(name)
        funcs.append(name)
    return structs, funcs


def _dirmap_makefile_srcs(makefile_text: str) -> List[str]:
    """Extract SRCS-style file lists from a BSD Makefile.

    BSD Makefiles list compilation inputs via SRCS=, KMOD=, and similar
    assignments, possibly continued across lines with a trailing '\\'.
    The output is informational ("here is what upstream actually
    builds"), so a best-effort regex is enough — we don't try to handle
    every conditional-include edge case.
    """
    srcs = []
    for m in re.finditer(
        r'^(SRCS|KMOD|PROG|PROGS|SUBDIR)\s*[+:]?=\s*((?:.|\\\n)*?)(?<!\\)$',
        makefile_text, re.MULTILINE,
    ):
        kind = m.group(1)
        body = m.group(2).replace('\\\n', ' ')
        items = [t for t in body.split() if t and not t.startswith('#')]
        if items:
            srcs.append(f"{kind}= {' '.join(items[:20])}")
    return srcs


class DirectoryMap(Tool):
    """Structured one-level summary of a FreeBSD source directory.

    Cheaper than reading every file. Returns subdirectories,
    Makefile build inputs, and per-file symbol summaries (struct
    names, function names, top-of-file comment) for .c and .h
    files. No recursion — call again on a subdir to drill in.

    Designed to give the writer agent the orientation a human gets
    from `ls` + `head` + `grep struct`, without burning multiple
    read_freebsd_source steps on files that turn out to be
    irrelevant.
    """

    name = "directory_map"
    description = (
        "Get a structured summary of one directory in the FreeBSD source "
        "tree. Returns: subdirectories, Makefile SRCS/KMOD lines, and for "
        "each .c/.h file the top-of-file comment plus struct and "
        "function names defined in it. One level only — no recursion. "
        "Use this BEFORE read_freebsd_source to find which file in a "
        "directory is worth reading. Examples: 'sys/vm', 'sys/kern', "
        "'sys/dev/usb'."
    )
    inputs = {
        "path": {
            "type": "string",
            "description": "Directory path relative to FreeBSD src root",
        }
    }
    output_type = "string"

    def forward(self, path: str) -> str:
        full, err = _safe_src_path(path)
        if err:
            return err
        if not os.path.isdir(full):
            return f"Not a directory: '{path}'"

        try:
            entries = sorted(os.listdir(full))
        except Exception as e:
            return f"Error listing {path}: {e}"

        subdirs = []
        c_h_files = []
        makefile = None
        for name in entries:
            fp = os.path.join(full, name)
            if os.path.isdir(fp):
                subdirs.append(name)
            elif name == 'Makefile' or name.startswith('Makefile.'):
                # Only one Makefile per dir is the common case; if
                # multiple exist (Makefile, Makefile.depend, etc.)
                # prefer the plain one.
                if makefile is None or name == 'Makefile':
                    makefile = name
            elif name.endswith('.c') or name.endswith('.h'):
                c_h_files.append(name)

        out_lines = [f"--- directory_map: {path} ---"]

        if subdirs:
            shown = subdirs[:40]
            extra = (
                f" (+{len(subdirs) - len(shown)} more)"
                if len(subdirs) > len(shown) else ""
            )
            out_lines.append(
                f"Subdirectories ({len(subdirs)}): "
                + ", ".join(shown) + extra
            )

        if makefile:
            try:
                mk_text = Path(os.path.join(full, makefile)).read_text(
                    errors="ignore"
                )
                srcs = _dirmap_makefile_srcs(mk_text)
                if srcs:
                    out_lines.append(f"\nMakefile build inputs ({makefile}):")
                    for line in srcs[:6]:
                        out_lines.append(f"  {line}")
            except Exception:
                pass  # Makefile read failure is non-fatal

        files_listed = 0
        files_skipped_size = 0
        if c_h_files:
            out_lines.append(
                f"\nSource files ({len(c_h_files)} .c/.h, "
                f"showing up to {_DIRMAP_FILE_LIMIT}):"
            )
            for name in c_h_files[:_DIRMAP_FILE_LIMIT]:
                fp = os.path.join(full, name)
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    continue
                if size > _DIRMAP_FILE_SIZE_CAP:
                    files_skipped_size += 1
                    continue
                try:
                    text = Path(fp).read_text(errors="ignore")
                except Exception:
                    continue
                header = _file_header_comment(text)
                structs, funcs = _dirmap_extract_names(text)
                line = f"\n  {name} ({size} bytes)"
                if header:
                    line += f"\n    purpose: {header}"
                if structs:
                    s_show = structs[:_DIRMAP_NAMES_PER_FILE]
                    s_extra = (
                        f" (+{len(structs) - len(s_show)} more)"
                        if len(structs) > len(s_show) else ""
                    )
                    line += "\n    structs: " + ", ".join(s_show) + s_extra
                if funcs:
                    f_show = funcs[:_DIRMAP_NAMES_PER_FILE]
                    f_extra = (
                        f" (+{len(funcs) - len(f_show)} more)"
                        if len(funcs) > len(f_show) else ""
                    )
                    line += "\n    functions: " + ", ".join(f_show) + f_extra
                out_lines.append(line)
                files_listed += 1

            if files_skipped_size:
                out_lines.append(
                    f"\n({files_skipped_size} file(s) skipped: too large — "
                    f"call read_freebsd_source explicitly if needed)"
                )
            if len(c_h_files) > _DIRMAP_FILE_LIMIT:
                out_lines.append(
                    f"\n(+{len(c_h_files) - _DIRMAP_FILE_LIMIT} more "
                    f"file(s) not shown — refine to a subdirectory)"
                )

        result = "\n".join(out_lines)
        if len(result) > _DIRMAP_OUTPUT_CAP:
            result = result[:_DIRMAP_OUTPUT_CAP] + "\n… (truncated)"
        return result


# ---------------------------------------------------------------------------
# 3d. Follow-Imports Tool (trace #include to resolve struct defs)
# ---------------------------------------------------------------------------


def _resolve_include_path(base_file: str, include_path: str,
                          src_root: str) -> str | None:
    """Resolve an #include path relative to the containing file.

    FreeBSD uses both absolute (from src root) and relative includes.
    Returns the resolved path relative to src_root, or None if not found.
    """
    base_dir = os.path.dirname(os.path.join(src_root, base_file))

    # Try as relative to the containing file's directory
    rel = os.path.normpath(os.path.join(base_dir, include_path))
    if os.path.exists(rel):
        return os.path.relpath(rel, src_root)

    # Try as absolute from src root (FreeBSD style: #include <sys/xxx.h>)
    if os.path.exists(os.path.join(src_root, include_path)):
        return include_path

    # Try with common FreeBSD include directories
    for inc_dir in ["sys", "include", "lib", "cddl", "usr.sbin"]:
        candidate = os.path.join(inc_dir, include_path)
        if os.path.exists(os.path.join(src_root, candidate)):
            return candidate

    return None


def _extract_includes(content: str) -> List[str]:
    """Extract #include paths from C source code."""
    includes = []
    for m in re.finditer(r'#include\s+["<]([^">]+)[">]', content):
        includes.append(m.group(1))
    return includes


def _extract_struct_defs(content: str, file_path: str) -> List[str]:
    """Extract struct definitions from C source code."""
    defs = []
    # Match: struct name { ... };
    for m in re.finditer(
        r'struct\s+(\w+)\s*\{([^}]+)\}',
        content,
        re.DOTALL
    ):
        name = m.group(1)
        body = m.group(2).strip()
        # Extract field names (simplified)
        fields = []
        for line in body.split('\n'):
            line = line.strip()
            if line and not line.startswith('//') and not line.startswith('/*'):
                # Skip typedef, enum, nested structs for simplicity
                if line.startswith('typedef') or line.startswith('enum'):
                    continue
                # Try to extract field name (rough heuristic)
                parts = line.split()
                if parts:
                    fields.append(parts[-1].rstrip(';,').rstrip('*'))
        defs.append(f"struct {name} (from {file_path}):\n  fields: {', '.join(fields[:10])}")
    return defs


def _extract_func_sigs(content: str, file_path: str) -> List[str]:
    """Extract function signatures from C source code.

    See _dirmap_extract_names for the multi-line regex rationale —
    same bug, same fix, anchored at column 0 so we don't pick up
    inline calls inside other function bodies.
    """
    sigs = []
    func_def_re = re.compile(
        r"^"
        r"(?:[A-Za-z_]\w*\s*\**\s+)+"      # return type + qualifiers
        r"\*?\s*([A-Za-z_]\w*)\s*"          # function name
        r"\([^{;]*?\)\s*"                   # arg list (may span lines)
        r"(?:__\w+(?:\s*\([^)]*\))?\s*)*"   # optional GCC attributes
        r"\{",
        re.MULTILINE | re.DOTALL,
    )
    for m in func_def_re.finditer(content):
        name = m.group(1)
        if name in ('if', 'else', 'while', 'for', 'switch', 'return',
                    'struct', 'union', 'enum', 'typedef', 'define'):
            continue
        sigs.append(f"{name} (from {file_path})")
    return sigs


class ResolveCDefinition(Tool):
    """Trace #include chains to find C struct/function/macro definitions.

    This tool follows #include directives through the FreeBSD source tree
    to locate where symbols are actually defined, helping the writer agent
    verify struct layouts, function signatures, and macro definitions.
    """

    name = "resolve_c_definition"
    description = (
        "Find the definition of a C struct, function, macro, or type alias "
        "in the FreeBSD source tree. Follows #include chains automatically. "
        "Returns the definition with file path and context. "
        "Examples: 'struct vm_page', 'uma_zcreate', 'VNET_DEFINE'"
    )
    inputs = {
        "symbol": {
            "type": "string",
            "description": "Symbol name to find (e.g., 'struct vm_page', 'uma_zcreate', 'VNET_DEFINE')",
        },
        "start_file": {
            "type": "string",
            "description": "Optional: start tracing #includes from this file (e.g., 'sys/vm/vm_map.c'). If omitted, searches entire tree.",
            "nullable": True,
        }
    }
    output_type = "string"

    def forward(self, symbol: str, start_file: str = "") -> str:
        """Search for and return the C definition of a symbol."""
        # Clean up symbol name
        symbol = symbol.strip()
        if symbol.startswith("struct "):
            struct_name = symbol[7:].strip()
            search_type = "struct"
        elif symbol.startswith("typedef ") or symbol.startswith("typedef"):
            # Extract the actual type name from typedef
            match = re.search(r'typedef\s+\w+\s+(\w+)', symbol)
            if match:
                symbol = match.group(1)
            search_type = "typedef"
        else:
            search_type = "general"

        results = []

        # Search strategy:
        # 1. If start_file provided, trace its #includes first
        # 2. Then search entire source tree

        files_to_search = set()

        start_full, start_err = _safe_src_path(start_file) if start_file else (None, None)
        if start_file and start_err is None and os.path.exists(start_full):
            # Trace #include chain from start file
            visited = set()
            queue = [start_file]
            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                files_to_search.add(current)

                try:
                    full_path, cur_err = _safe_src_path(current)
                    if cur_err:
                        continue
                    content = Path(full_path).read_text(errors="ignore")
                    includes = _extract_includes(content)
                    for inc in includes:
                        resolved = _resolve_include_path(current, inc, SRC_ROOT)
                        if resolved and resolved not in visited:
                            queue.append(resolved)
                except Exception:
                    pass

        # 2. Search entire source tree for the symbol
        # (only if we haven't already covered it)
        if not files_to_search or search_type == "struct":
            # Search for struct definitions
            for root, dirs, files in os.walk(os.path.join(SRC_ROOT, "sys")):
                for fname in files:
                    if not (fname.endswith('.c') or fname.endswith('.h')):
                        continue
                    fpath = os.path.relpath(os.path.join(root, fname), SRC_ROOT)
                    if fpath in files_to_search:
                        continue
                    try:
                        content = Path(os.path.join(root, fname)).read_text(errors="ignore")
                        if search_type == "struct":
                            if f'struct {struct_name}' in content or f'struct {struct_name} ' in content:
                                files_to_search.add(fpath)
                        elif search_type == "general":
                            # Search for function, macro, or typedef
                            patterns = [
                                rf'\b{re.escape(symbol)}\b',
                                rf'\b{re.escape(symbol)}\s*\(',
                                rf'\b{re.escape(symbol)}\s*\{{',
                            ]
                            for pattern in patterns:
                                if re.search(pattern, content):
                                    files_to_search.add(fpath)
                                    break
                    except Exception:
                        continue

        # 3. Extract definitions from found files
        found_defs = []
        for fpath in sorted(files_to_search):
            try:
                full_path = os.path.join(SRC_ROOT, fpath)
                content = Path(full_path).read_text(errors="ignore")

                if search_type == "struct":
                    struct_defs = _extract_struct_defs(content, fpath)
                    for sd in struct_defs:
                        if struct_name in sd:
                            found_defs.append(sd)
                elif search_type == "general":
                    # Search for the symbol in various forms
                    func_sigs = _extract_func_sigs(content, fpath)
                    for fs in func_sigs:
                        if symbol in fs:
                            found_defs.append(fs)

                    # Also search for #define macros.
                    # Match BOTH bare macros (`#define FOO 1`) and
                    # parameterized macros (`#define FOO(a, b) ...`).
                    # The previous regex required `\s+` after the symbol
                    # name, which silently missed every parameterized
                    # macro in the tree — e.g. VNET_DEFINE(t, n),
                    # CURVNET_SET(arg), IFLIB_CTX(...). The tool then
                    # returned "No exact definition found" for symbols
                    # that are real, well-defined macros, and the
                    # reviewer would flag them as hallucinated. Capture
                    # everything after the symbol so the rest of the line
                    # (parameter list + body) is preserved verbatim.
                    for m in re.finditer(
                        rf'#define\s+{re.escape(symbol)}([(\s].*)',
                        content
                    ):
                        macro_tail = m.group(1).strip()[:200]
                        found_defs.append(f"#define {symbol}{macro_tail if macro_tail.startswith('(') else ' ' + macro_tail} (from {fpath})")

            except Exception:
                continue

        if not found_defs:
            # Try a broader search - just grep for the symbol
            broader_results = []
            for root, dirs, files in os.walk(os.path.join(SRC_ROOT, "sys")):
                for fname in files[:50]:  # Limit to avoid slow walks
                    if not (fname.endswith('.c') or fname.endswith('.h')):
                        continue
                    fpath = os.path.relpath(os.path.join(root, fname), SRC_ROOT)
                    try:
                        content = Path(os.path.join(root, fname)).read_text(errors="ignore")
                        if search_type == "struct":
                            if f'struct {struct_name}' in content:
                                broader_results.append(fpath)
                        else:
                            if re.search(rf'\b{re.escape(symbol)}\b', content):
                                broader_results.append(fpath)
                    except Exception:
                        continue
                if broader_results:
                    break  # Found something, stop

            if broader_results:
                return (
                    f"No exact definition found for '{symbol}', "
                    f"but it appears in these files:\n" +
                    "\n".join(f"  - {f}" for f in broader_results[:10]) +
                    "\n\nTry reading one of these files with read_freebsd_source."
                )

            return f"Could not find definition for '{symbol}' in {SRC_ROOT}"

        return f"=== Definition for '{symbol}' ===\n" + "\n".join(found_defs[:15])


def gather_source_context(chapter: dict) -> str:
    """Read existing documentation in the target area for context.

    Returns a string with existing README content and kerneldoc descriptions.
    Empty string if nothing found.
    """
    output_file = chapter.get("output_file", "README.md")
    output_dir = os.path.dirname(os.path.join(SRC_ROOT, output_file))
    parts = []

    # 1. Read existing README in target directory
    existing_readme = os.path.join(output_dir, "README.md")
    if os.path.exists(existing_readme):
        try:
            with open(existing_readme) as f:
                content = f.read()
            # Skip our own backup marker
            if "freebsd-docs.bak" not in content:
                parts.append(f"## Existing README in target directory ({output_dir}/README.md)\n\n```\n{content[:5000]}\n```\n")
        except Exception:
            pass

    # 2. Read sys/README.md for kernel chapters
    sys_readme = os.path.join(SRC_ROOT, "sys", "README.md")
    if os.path.exists(sys_readme) and "sys/" in output_file:
        try:
            with open(sys_readme) as f:
                content = f.read()
            parts.append(f"## sys/README.md (kernel roadmap)\n\n```\n{content[:5000]}\n```\n")
        except Exception:
            pass

    # 3. Read kerneldoc Doxyfile descriptions
    kerneldoc_dir = os.path.join(SRC_ROOT, "tools", "kerneldoc")
    if os.path.isdir(kerneldoc_dir):
        for fname in sorted(os.listdir(kerneldoc_dir)):
            if fname.startswith("Doxyfile-") and fname.endswith(".md"):
                fpath = os.path.join(kerneldoc_dir, fname)
                try:
                    with open(fpath) as f:
                        content = f.read()
                    parts.append(f"## Kerneldoc: {fname}\n\n```\n{content[:3000]}\n```\n")
                except Exception:
                    pass

    # 4. Read Doxyfile-dev_* for subsystem descriptions
    for fname in sorted(os.listdir(kerneldoc_dir)) if os.path.isdir(kerneldoc_dir) else []:
        if fname.startswith("Doxyfile-dev-") and fname.endswith(".md"):
            fpath = os.path.join(kerneldoc_dir, fname)
            try:
                with open(fpath) as f:
                    content = f.read()
                parts.append(f"## Kerneldoc dev: {fname}\n\n```\n{content[:3000]}\n```\n")
            except Exception:
                pass

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 4. Chapter Prompt Builder
# ---------------------------------------------------------------------------


# Single source of truth for the "no marketing language" ban. The writer
# prompt warns against these; the reviewer prompt FAILs on them. Previously
# each prompt carried its own hand-maintained copy and they had already
# drifted (the writer forbade "advanced" but the reviewer's list omitted it,
# so it was instructed-but-unenforced). Both prompts now interpolate this
# constant, so the writer's warning and the reviewer's gate can never
# disagree again. Keep the phrasing free of double quotes — it is embedded
# in f-strings and, downstream, in the reviewer's JSON-emitting prompt.
_FORBIDDEN_MARKETING_WORDS = (
    "comprehensive, robust, seamless, seamlessly, leverage, leveraging, "
    "cutting-edge, state-of-the-art, elegant, powerful, simply, easily, "
    "effortlessly, blazing-fast, world-class, best-in-class, "
    "industry-leading, rich set of, wide range of, wide variety of, "
    "tight integration, deep integration, first-class, rock-solid, "
    "battle-tested, modern, sophisticated, advanced"
)


# Section catalog. Each entry maps the H2 header name to:
#   - template_body: prose shown inside the writer's output template, telling
#     the model what content goes there.
#   - rubric_body: the one-line description used in the reviewer's structure
#     rubric checklist.
# Adding a section here makes it available to chapters.yaml — chapters opt in
# via a `sections:` list. The default list (below) is what existing chapters
# get when they don't specify one, preserving prior behaviour.
_SECTION_CATALOG = {
    "Quick Summary": {
        "template_body": (
            "(3-4 paragraphs: what this subsystem does and why it matters.\n"
            "No code — accessible to any reader who knows C.)"
        ),
        "rubric_body": "3-4 paragraphs, no code (beginners)",
    },
    "Glossary": {
        # In _DEFAULT_SECTIONS since 2026-08-29, positioned immediately
        # after Quick Summary: defining terms AFTER the Architecture
        # section has already used them defeats the point.
        #
        # Promoted from opt-in because the mechanical scan found 5.2
        # undefined terms per chapter across the chapters shipped that
        # week, with 0 of 5 carrying a Glossary at all. The same handful
        # recurred everywhere (newbus, devclass, kld, sysinit, UMA) — each
        # chapter is written in isolation, so every one of them re-meets
        # the same vocabulary cold. Telling the reviewer about undefined
        # terms (step 1) did not fix it; giving the writer a section to
        # put them in is the other half.
        "template_body": (
            "(3-8 single-line definitions of terms used in this chapter\n"
            "that a junior developer wouldn't already know — e.g. TLB\n"
            "shootdown, PML4, copy-on-write, slab, shadow chain, NUMA\n"
            "domain. One sentence per term, in the form `**term** —\n"
            "definition.`. Skip terms a working C programmer already\n"
            "knows (pointer, struct, mutex). Cover only terms that\n"
            "actually appear later in this chapter.)"
        ),
        "rubric_body": "3-8 one-line definitions of jargon used in chapter",
    },
    "Architecture": {
        "template_body": "(technical explanation with specific source file references)",
        "rubric_body": "technical explanation with source references",
    },
    "Key Data Structures": {
        "template_body": "(C structs with field explanations, quoting from actual header files)",
        "rubric_body": "C structs with field explanations",
    },
    "Deep Dive": {
        "template_body": (
            "(Source code walkthrough: trace through key functions step-by-step,\n"
            "referencing specific files with code snippets. This is the intermediate\n"
            "reading level.)"
        ),
        "rubric_body": "source code walkthrough with code snippets",
    },
    "Flow / Diagram": {
        # `{diagram}` is filled in at format-time by the caller.
        "template_body": "(Mermaid {diagram} diagram — valid syntax, not a placeholder)",
        "rubric_body": "valid Mermaid diagram (not placeholder)",
    },
    "Advanced Notes": {
        "template_body": (
            "(Practical insights for advanced readers: debugging with DTrace,\n"
            "performance implications, race conditions, common pitfalls,\n"
            "connection to OS theory from textbooks.)"
        ),
        "rubric_body": "DTrace, performance, pitfalls (advanced)",
    },
    "See Also": {
        "template_body": "(related chapters and source directories to explore next)",
        "rubric_body": "related chapters/directories",
    },
}

# Default section order, matching the original 7 H2 sections (plus the H1
# title that's added separately by the prompt). Chapters that don't declare
# a `sections:` list get this set, preserving backward-compatible behaviour
# for chapters defined before the per-chapter override existed.
#
# The mandatory `## Comparison` section was removed in 2026-05: it was the
# dominant source of unverifiable hallucinations (cross-OS claims that the
# deterministic FreeBSD-source fact-checker cannot grade), and the writer
# has no way to verify them either. Chapters that legitimately benefit from
# a small in-line analogy can include it within Architecture or Advanced
# Notes; a separately-graded section produced more harm than good.
#
# `Glossary` joined the defaults on 2026-08-29 (see the catalog entry for
# the measurement that motivated it). It sits second, right after Quick
# Summary, so terms are defined before Architecture starts using them.
# This takes the default chapter from 7 H2 sections to 8 — the reviewer's
# structure criterion counts sections, so its rubric text is generated
# from this list rather than hard-coded.
_DEFAULT_SECTIONS = [
    "Quick Summary", "Glossary", "Architecture", "Key Data Structures",
    "Deep Dive", "Flow / Diagram", "Advanced Notes", "See Also",
]


def _chapter_sections(chapter: dict) -> list:
    """Resolve a chapter's section list, validating against the catalog.

    A chapter may opt out of irrelevant sections (e.g. a tree-overview
    chapter has no specific structs to feature) by declaring
    `sections: [Quick Summary, Architecture, ...]` in chapters.yaml.
    Unknown names are dropped with a warning rather than crashing the run —
    a typo in YAML shouldn't kill an otherwise-good chapter.
    """
    raw = chapter.get("sections")
    if not raw:
        return list(_DEFAULT_SECTIONS)
    resolved = []
    for name in raw:
        if name in _SECTION_CATALOG:
            resolved.append(name)
        else:
            print(f"  ⚠ chapter {chapter.get('title')!r}: unknown section "
                  f"{name!r} — dropping (valid: {list(_SECTION_CATALOG)})")
    if not resolved:
        # An entirely-bogus list shouldn't leave the writer with nothing to
        # produce. Fall back to the default set.
        return list(_DEFAULT_SECTIONS)
    return resolved


# --- Symbol catalog (writer-prompt ground truth) ---------------------------
#
# The writer hallucinates plausible-but-fake symbol names because its
# training data is dense with FreeBSD-shaped code from neighbouring OSes.
# `read_freebsd_source` and `resolve_c_definition` exist to ground it,
# but the writer only reaches for them when it already knows what to
# look up — for novel names it just emits whatever feels right.
#
# The fix is to pre-compute the chapter's authoritative symbol surface
# (struct + function names defined in the chapter's source files) and
# inject it into the prompt. With a real shortlist in front of it, the
# writer reaches for those names instead of inventing.

# Per-chapter catalog budget. We cap aggregated symbols at ~600 names
# (≈ 6 KB at 10 chars/symbol) to keep the writer prompt under control.
# Symbols are listed alphabetically; if we hit the cap, the catalog
# notes the truncation so the writer doesn't take the omission as
# "those symbols don't exist."
_CATALOG_MAX_STRUCTS = 200
_CATALOG_MAX_FUNCS = 400
# Per-file size cap mirrors directory_map's — generated files
# (linker_set.h-ish) are 100 KB+ of irrelevant noise.
_CATALOG_FILE_SIZE_CAP = 256 * 1024
# Per-dir file cap for source_dirs. Keeps a chapter that lists
# `sys/kern/` as a dir from dragging in 600 files.
_CATALOG_FILES_PER_DIR = 30


def _build_symbol_catalog(chapter: dict) -> str:
    """Build the authoritative struct/function catalog for a chapter.

    Walks the chapter's `source_files` (read each fully) and a bounded
    sample of files inside each `source_dirs` entry, aggregates
    struct names and function names with `_dirmap_extract_names`, and
    returns a Markdown block ready to interpolate into the writer
    prompt. Returns "" if the chapter has no source-tree pointers.

    The catalog is the source of truth the writer should reach for
    FIRST before guessing a struct or function name. It is not
    exhaustive — real FreeBSD touches symbols defined in headers we
    didn't sample — but it is correct: every name in the catalog has
    a `struct NAME {` or function-definition line in the listed file.
    """
    src_files = chapter.get("source_files", []) or []
    src_dirs = chapter.get("source_dirs", []) or []
    if not src_files and not src_dirs:
        return ""

    structs: set = set()
    funcs: set = set()

    def harvest(full_path: str):
        try:
            sz = os.path.getsize(full_path)
        except OSError:
            return
        if sz > _CATALOG_FILE_SIZE_CAP:
            return
        try:
            with open(full_path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return
        s_names, f_names = _dirmap_extract_names(content)
        structs.update(s_names)
        funcs.update(f_names)

    for rel in src_files:
        if not (rel.endswith(".c") or rel.endswith(".h")):
            continue  # README, Makefile, etc. have no C symbols
        full = os.path.join(SRC_ROOT, rel)
        if os.path.isfile(full):
            harvest(full)

    for rel in src_dirs:
        full = os.path.join(SRC_ROOT, rel)
        if not os.path.isdir(full):
            continue
        try:
            entries = sorted(os.listdir(full))
        except OSError:
            continue
        c_h = [n for n in entries
               if n.endswith(".c") or n.endswith(".h")]
        for name in c_h[:_CATALOG_FILES_PER_DIR]:
            harvest(os.path.join(full, name))

    if not structs and not funcs:
        return ""

    s_sorted = sorted(structs)
    f_sorted = sorted(funcs)
    s_truncated = len(s_sorted) > _CATALOG_MAX_STRUCTS
    f_truncated = len(f_sorted) > _CATALOG_MAX_FUNCS
    s_sorted = s_sorted[:_CATALOG_MAX_STRUCTS]
    f_sorted = f_sorted[:_CATALOG_MAX_FUNCS]

    parts = [
        "## Authoritative Symbol Catalog",
        "",
        "These struct names and function names have been extracted from",
        "the chapter's source files. Every name here is REAL — the symbol",
        "is defined in one of the listed files. Reach for THESE names",
        "first. If you need a name in this domain that isn't on the list,",
        "call `read_freebsd_source` or `resolve_c_definition` to verify",
        "before writing it — do NOT guess from training-data familiarity.",
        "",
        "This list is *not exhaustive*: real FreeBSD touches symbols",
        "defined in headers not sampled here. Absence from the list is",
        "NOT proof a name is fake — but presence IS proof a name is",
        "real. Treat it as a shortlist of safe choices, not a closed set.",
        "",
    ]
    if s_sorted:
        parts.append(f"**Verified structs ({len(s_sorted)}"
                     f"{' truncated' if s_truncated else ''}):**")
        # Wrap the names into rows of ~6 names each to keep token cost
        # predictable. The writer doesn't need clickable lists; it needs
        # a glance-able catalog.
        for i in range(0, len(s_sorted), 6):
            parts.append("  " + ", ".join(f"`{s}`" for s in s_sorted[i:i+6]))
        parts.append("")
    if f_sorted:
        parts.append(f"**Verified functions ({len(f_sorted)}"
                     f"{' truncated' if f_truncated else ''}):**")
        for i in range(0, len(f_sorted), 6):
            parts.append("  " + ", ".join(f"`{f}()`" for f in f_sorted[i:i+6]))
        parts.append("")
    return "\n".join(parts)


def build_chapter_prompt(chapter: dict) -> str:
    """Build the instruction prompt for the writer agent."""
    src_files = chapter.get("source_files", [])
    src_dirs = chapter.get("source_dirs", [])
    focus = chapter.get("focus", "")
    questions = chapter.get("key_questions", [])
    diagram = chapter.get("mermaid", "flowchart")
    scope_guard = (chapter.get("scope_guard") or "").strip()

    # Gather existing documentation context
    source_context = gather_source_context(chapter)

    steps = []
    step_n = 1

    if source_context:
        steps.append(
            f"STEP {step_n}: Read the existing documentation below for context "
            f"about what's already documented. Do NOT duplicate content — extend "
            f"and complement what exists.\n\n{source_context}"
        )
        step_n += 1
    elif src_dirs:
        steps.append(
            f"STEP {step_n}: Use explore_tree to survey these directories:\n"
            + "\n".join(f"        - {d}" for d in src_dirs)
        )
        step_n += 1
    else:
        steps.append(f"STEP {step_n}: Understand the context from the files below.")
        step_n += 1

    if src_dirs:
        steps.append(
            f"STEP {step_n}: For each directory, call directory_map(path=...) "
            "BEFORE reading files. directory_map returns one structured\n"
            "        block listing subdirs, Makefile build inputs, and per-file\n"
            "        struct/function names. Use it to pick which files are\n"
            "        worth a full read_freebsd_source — this avoids burning\n"
            "        steps on files that turn out to be irrelevant. Examples:\n"
            + "\n".join(f"        - directory_map(path='{d}')" for d in src_dirs[:3])
        )
        step_n += 1

    if src_files:
        steps.append(
            f"STEP {step_n}: Use read_freebsd_source to examine key files:\n"
            + "\n".join(f"        - {f}" for f in src_files)
        )
        step_n += 1

    steps.append(
        f"STEP {step_n}: Use search_books to find architectural theory and "
        "historical context for the concepts covered in this chapter."
    )
    step_n += 1

    steps.append(
        f"STEP {step_n}: Use resolve_c_definition to verify struct layouts, "
        "function signatures, and macro definitions. For example:\n"
        "  - resolve_c_definition(symbol='struct vm_page')\n"
        "  - resolve_c_definition(symbol='uma_zcreate', start_file='sys/vm/uma_core.c')\n"
        "This tool follows #include chains to find actual definitions."
    )
    step_n += 1

    # Diagram is only required if the chapter actually includes the
    # "Flow / Diagram" section. Chapters that opt out via `sections:` in
    # chapters.yaml should not be told to produce a diagram, otherwise
    # the writer creates one anyway and the reviewer flags it on every
    # revision round.
    sections_for_writer = _chapter_sections(chapter)
    wants_diagram = "Flow / Diagram" in sections_for_writer

    question_text = "\n".join(f"    - {q}" for q in questions)
    requirements = [
        "    - **Quick Summary** — 3-4 paragraphs, no code (beginners)",
        "    - **Deep Dive** — source code walkthrough, struct analysis (intermediate)",
        "    - **Advanced Notes** — debugging with DTrace, performance, pitfalls (advanced)",
        f"    - Addresses these key questions:\n{question_text}",
    ]
    if wants_diagram:
        requirements.append(f"    - Includes a Mermaid {diagram} diagram (see below)")
    requirements.extend([
        "    - References specific source files with line-level details",
        "    - Connects theory (from books) to implementation (from source)",
    ])
    steps.append(
        f"STEP {step_n}: Write a README.md with three reading levels:\n"
        + "\n".join(requirements)
    )

    if wants_diagram:
        diagram_hints = {
            "sequence": (
                "    - Mermaid sequence diagram: show the flow of control/data\n"
                "      between components (e.g., [UEFI] → [loader] → [kernel])\n"
                "      Use: ```mermaid\\nsequenceDiagram\\n  Participant A\\n  A->>B: action\n"
                "      ```"
            ),
            "flowchart": (
                "    - Mermaid flowchart: show the data flow or component hierarchy\n"
                "      Use: ```mermaid\\nflowchart TD\\n  A[Component] --> B[Subcomponent]\n"
                "      ```\n"
                "      IMPORTANT — node ids and subgraph ids share one namespace.\n"
                "      Do NOT reuse a node id as a subgraph id. Mermaid then tries\n"
                "      to make the node a child of a subgraph with the same name\n"
                "      and refuses to render with: \"Setting <X> as parent of <X>\n"
                "      would create a cycle\". If you want a subgraph that visually\n"
                "      groups the `Userland` node, name the subgraph `UserlandGroup`\n"
                "      (or any unused id) and let the title carry the human-readable\n"
                "      label: `subgraph UserlandGroup [\"Userland\"]`."
            ),
            "class": (
                "    - Mermaid class diagram: show key structs and their relationships\n"
                "      Use: ```mermaid\\nclassDiagram\\n  class StructName {\\n    +field type\\n  }\n"
                "      ```\n"
                "      IMPORTANT — Mermaid classDiagram syntax is STRICTER than C:\n"
                "      * One field per line, format: `+field_name type` or `+type field_name`\n"
                "      * NO nested braces in members. Do NOT write\n"
                "        `+union { struct foo bar; } u` — Mermaid parses the inner `{`\n"
                "        as a new class body and errors with\n"
                "        \"Expecting STRUCT_STOP, got OPEN_IN_STRUCT\". Either flatten\n"
                "        the union member to its dominant case (e.g. `+vm_pagequeue pq`)\n"
                "        or omit it.\n"
                "      * NO parentheses inside member declarations. Do NOT write\n"
                "        `+TAILQ_ENTRY(vm_object) object_list`,\n"
                "        `+LIST_HEAD(, vm_object) shadow_head`, or\n"
                "        `+void (*pmap_enter)(vm_offset_t, vm_prot_t)`.\n"
                "        Replace macro-typed list members with a simple typed name\n"
                "        (`+vm_object_link object_list`); for function-pointer members,\n"
                "        write the operation name only (`+pmap_enter()`).\n"
                "      * NO commas, no semicolons, no `*` for pointer asterisks inside\n"
                "        the brace block. Use `+vm_object obj` not `+struct vm_object *obj`.\n"
                "      * Keep each class to ~6-10 fields max — pick the load-bearing\n"
                "        ones, not every field in the C struct.\n"
                "      Verify the diagram renders by mentally parsing each line as\n"
                "      `+IDENT IDENT` only. If a line needs anything else, simplify it."
            ),
            "state": (
                "    - Mermaid state diagram: show state transitions\n"
                "      Use: ```mermaid\\nstateDiagram-v2\\n  [*] --> Idle\\n  Idle --> Active: event\n"
                "      ```"
            ),
        }

        steps.append(f"    - Diagram format hint:{diagram_hints.get(diagram, '')}")

    # Build the section template from the chapter's section list. Each
    # chapter may opt out of irrelevant sections via `sections:` in
    # chapters.yaml — see _chapter_sections().
    #
    # Indentation gymnastics: the f-string below sits inside a
    # textwrap.dedent block whose common leading whitespace is 8 spaces.
    # The placeholder `{template_body}` is itself preceded by 8 spaces of
    # f-string scaffolding, which become the first line's indent. Every
    # *other* body line therefore needs 8 leading spaces of its own to
    # land at the same column after dedent. Pre-indenting only the lines
    # *after* the first achieves that.
    sections = sections_for_writer
    template_blocks = []
    for name in sections:
        body = _SECTION_CATALOG[name]["template_body"].format(diagram=diagram)
        template_blocks.append(f"## {name}\n{body}")
    raw_body = "\n\n".join(template_blocks)
    first, sep, rest = raw_body.partition("\n")
    template_body = first + (sep + textwrap.indent(rest, "        ") if sep else "")

    # Optional per-chapter scope guard (chapters.yaml `scope_guard:`).
    # Same indentation rule as template_body — first line is already at the
    # f-string scaffolding column; pre-indent every subsequent line.
    if scope_guard:
        sg_first, sg_sep, sg_rest = scope_guard.partition("\n")
        sg_body = sg_first + (
            sg_sep + textwrap.indent(sg_rest, "        ") if sg_sep else ""
        )
        scope_guard_block = (
            "\n        ## Scope Guard\n"
            "        (HARD RULE — do not violate, even if a referenced file tempts you to.)\n"
            f"        {sg_body}\n"
        )
    else:
        scope_guard_block = ""

    # Per-chapter symbol catalog — pre-extract real struct/function
    # names from the chapter's source files so the writer can reach
    # for them instead of guessing names that "feel right" from
    # neighbouring-OS training data. Same indentation rule as the
    # other interpolated blocks: first line lands at the f-string
    # scaffolding column, every subsequent line needs pre-indent.
    catalog_raw = _build_symbol_catalog(chapter)
    if catalog_raw:
        cat_first, cat_sep, cat_rest = catalog_raw.partition("\n")
        cat_body = cat_first + (
            cat_sep + textwrap.indent(cat_rest, "        ") if cat_sep else ""
        )
        catalog_block = f"\n        {cat_body}\n"
    else:
        catalog_block = ""

    return textwrap.dedent(f"""\
        You are writing a chapter for "FreeBSD Internals" — a
        guide that helps anyone interested in operating systems understand
        how they work by studying real FreeBSD source code.

        ## Chapter: {chapter['title']}

        ## Focus
        {focus}
        {scope_guard_block}{catalog_block}
        ## Instructions
        {chr(10).join(f"{s}" for s in steps)}

        ## Mandatory Output Template

        You MUST output ALL of the following sections in this exact order.
        Do NOT skip any section. Do NOT add sections outside this template.
        Each section must have substantive content (at least 3-5 sentences).

        ---BEGIN TEMPLATE---

        # {chapter['title']}

        {template_body}

        ---END TEMPLATE---

        **Rules:**
        - Always reference specific file paths (e.g., `sys/vm/vm_page.c`)
        - Include C code snippets where they illuminate the design
        - Connect textbook theory to actual FreeBSD implementation
        - Make it accessible to any reader who knows C but not kernel internals
        - Output ONLY the Markdown content — no preamble, no explanation
        - EVERY section header above MUST appear in your output
        - If you cannot fill a section with real content, write "See related
          chapters for coverage of this topic" rather than skipping it

        **Quote, don't paraphrase — READ THIS BEFORE WRITING ANY STRUCT OR FUNCTION.**
        Your training data contains FreeBSD-shaped code that is *almost*
        right but mixes in field names, function names, and macros from
        Linux, NetBSD, OpenBSD, and older FreeBSD versions. If you write
        a struct definition or a function signature from memory, it WILL
        contain hallucinated fields (e.g. `b_dirtyblkhd`, `b_actf`,
        `bufq_insert_dirty()`, `MALLOC_DEFINE(M_BIOBUF, ...)`). The
        reviewer cannot rescue a draft that fabricates structurally —
        revisions can only patch, not rewrite.
        - For ANY struct definition, field name, or function signature
          you put in a code block: first call
          `read_freebsd_source(path="sys/sys/foo.h")` (or
          `resolve_c_definition(symbol="struct foo")`), then COPY the
          relevant lines verbatim into the code block. Do not retype
          from memory.
        - For SDT probes, `MALLOC_DEFINE`/`MALLOC_DECLARE` tags, sysctl
          names, and macro names: same rule — verify by reading the
          source before claiming the name exists. If `read_freebsd_source`
          / `resolve_c_definition` doesn't surface it, do not write it.
        - Prose around the code block can paraphrase freely; the rule is
          for the code block contents and any inline backticked symbol
          claims (e.g. mentioning a specific field name, function name,
          or sysctl OID by name).

        **Man-page references — always use `name(N)`.** The canonical
        FreeBSD form is `name(section)` with parentheses around the
        section number: `mbuf(9)`, `pf.conf(5)`, `jail(8)`. Do NOT
        write the dotted-filename form (`mbuf.9`, `g_bio.9`) and do
        NOT pre-wrap them as markdown links — a post-processor turns
        every bare `name(N)` into a clickable relative link to the
        mdoc source under `share/man/manN/`, but only if you emit the
        canonical form. Backticks around the ref are fine and survive
        the link wrapper (`` `mbuf(9)` `` → `` [`mbuf(9)`](path) ``);
        anything else is left as plain text.
        - WRONG: ``- **Man pages**: `g_bio.9`, `g_geom.9` `` (dotted
          form — the autolinker can't see these).
        - WRONG: ``- [VNET(9)](../../share/man/man9/VNET.9)`` (you
          guessed the path; the autolinker computes the correct
          relative path from the chapter's location).
        - RIGHT: ``- Man pages: `g_bio(9)`, `g_geom(9)`, `lock(9)` ``
          (the post-processor links each one).

        **How to return your work — READ THIS CAREFULLY:**
        - You MUST return the complete Markdown chapter as the single argument
          to `final_answer(...)`. Example: `final_answer(content)` where
          `content` is the full chapter string.
        - There is NO file-write tool available to you. `open()`, `Path.write_text`,
          `os.write`, and any other file I/O are FORBIDDEN and will raise
          `InterpreterError`. Do NOT try to write `/tmp/chapter.md`,
          `output.md`, or any other file — the pipeline writes the file for
          you AFTER you return.
        - Do NOT return a status string like "README.md successfully written".
          That is not the chapter — that is a summary. Return the actual
          Markdown body, headers and all, starting with `# {chapter['title']}`.
        - If your code execution hits a `Forbidden function evaluation` error
          for `open` or similar, that is a signal to stop trying to write
          files and call `final_answer(content)` directly with the content
          string you already built.

        **Code blocks must be Python — NOT shell.** Your code blocks are
        executed by a Python interpreter, not a shell. Lines like
        `grep -n "FOO" file | head -50`, `cat file`, `ls dir/`, or any
        bare command-line invocation will fail with
        `SyntaxError: invalid syntax` and waste a step. To search source,
        use the provided tools: `directory_map(path=...)` for a
        structured summary of one directory (subdirs, file purposes,
        struct/function names), `read_freebsd_source(path=...)` to read
        a whole file, `search_books(query=...)` for theory, and
        `resolve_c_definition(symbol=...)` for struct/function lookups.
        If you need to filter the text of a file you've already read,
        do it in Python (e.g., `[l for l in text.splitlines() if "SUBDIR" in l]`),
        not via shell pipes.

        **No marketing language.** This is a technical reference, not a
        product brochure. Forbidden words and phrases — do NOT use any of
        them, even rephrased: {_FORBIDDEN_MARKETING_WORDS}. ("advanced" is
        banned as a generic adjective; the section title "Advanced Notes"
        is fine.) If a sentence relies on one of these words to be
        impressive, the sentence is empty — replace it with a concrete
        fact or delete it.

        **Explain WHY a non-obvious structure exists, not just what it
        is.** When you introduce a non-trivial data structure or mechanism
        the FIRST time it appears in the chapter — shadow chains, inactive
        queues, UMA kegs vs zones, pagedaemon thresholds, witness, turnstiles,
        copy-on-write, slab caches, NUMA domains, etc. — give one sentence
        on the engineering problem it solves. The reader needs the *reason*
        before the *mechanism* will stick.
        - WRONG: "A shadow chain is a linked list of vm_objects."
          (Tells the reader what it looks like, not why it exists.)
        - RIGHT: "Shadow chains let fork() share pages copy-on-write — the
          child gets a new vm_object whose shadow points at the parent's,
          so only pages the child actually modifies need to be duplicated."
          (Names the problem — fork() is expensive if pages are eagerly
          copied — and the trade-off the design buys.)
        This rule applies to *non-obvious* structures only. A `struct
        proc` doesn't need a "why processes exist" sentence; a `vm_map`'s
        red-black tree doesn't need a "why O(log n) is fast" sentence.
        Apply it where a junior reader would reasonably ask "but why is
        it built that way?" — typically once per major mechanism, not
        once per struct.
    """).lstrip()


def build_review_prompt(chapter: dict, draft: str) -> str:
    """Build the review prompt for the reviewer agent."""
    questions = chapter.get("key_questions", [])
    src_files = chapter.get("source_files", [])
    src_dirs = chapter.get("source_dirs", []) or []
    diagram = chapter.get("mermaid", "flowchart")
    scope_guard = (chapter.get("scope_guard") or "").strip()
    question_text = "\n".join(f"- {q}" for q in questions)

    # Mirror the writer's scope_guard into the reviewer's rubric so the
    # reviewer doesn't penalise the draft for content the writer was
    # explicitly told to suppress. Observed on chapter 1
    # (`README_internals.md`): the writer dropped struct walkthroughs
    # per scope_guard, then the reviewer's source_coverage criterion
    # graded them as missing.
    if scope_guard:
        sg_first, sg_sep, sg_rest = scope_guard.partition("\n")
        sg_body = sg_first + (
            sg_sep + textwrap.indent(sg_rest, "        ") if sg_sep else ""
        )
        scope_guard_block = (
            "## Scope Guard (the writer was told to honor this — so should you)\n\n"
            "The chapter author was given the following hard scope rule. Do\n"
            "NOT flag the draft as incomplete or under-covered for omitting\n"
            "content this rule forbids. Apply Source Coverage and Completeness\n"
            "ONLY to material that falls *inside* the scope.\n\n"
            f"{sg_body}\n\n"
        )
    else:
        scope_guard_block = ""

    # Pre-validate every source path so we can inject ground truth and
    # block the reviewer from hallucinating "this file does not exist."
    # The reviewer agent has no source-tree access (its only tool is
    # search_books). When the writer correctly cites a file, the
    # reviewer otherwise has no way to verify presence — and we've
    # observed it confidently asserting non-existence of real files
    # (e.g., sys/kern/kern_thread.c), which then poisons the revision
    # loop because the writer is told to "fix" a non-issue.
    verified_paths: List[str] = []
    missing_paths: List[str] = []
    for path in list(src_files) + list(src_dirs):
        full = os.path.join(SRC_ROOT, path)
        if os.path.exists(full):
            verified_paths.append(path)
        else:
            missing_paths.append(path)
    if verified_paths:
        verified_block = (
            "## Verified Source Paths (DO NOT claim these don't exist)\n\n"
            "The following paths have been confirmed to exist in the\n"
            "FreeBSD source tree. If the draft cites any of them, treat\n"
            "the citation as path-correct. You may still flag *misuse*\n"
            "(e.g., wrong subsystem, wrong content) but NOT non-existence.\n\n"
            + "\n".join(f"- `{p}` (exists)" for p in verified_paths)
            + "\n"
        )
    else:
        verified_block = ""
    if missing_paths:
        verified_block += (
            "\nNote: the following expected paths are missing from the\n"
            "tree and SHOULD be flagged if the draft cites them as if real:\n\n"
            + "\n".join(f"- `{p}` (NOT FOUND)" for p in missing_paths)
            + "\n"
        )

    # Pre-validate symbols (structs and functions) the same way we
    # pre-validate paths. The reviewer agent has no source-tree tool,
    # so without injected ground truth it can only hedge ("may use X /
    # verify against") on any specific symbol the writer claims. Hedges
    # turn into bad revisions: the writer guesses whichever name the
    # reviewer mentioned and introduces fresh hallucinations.
    #
    # We reuse the same extractors and verifiers that fact_check_draft
    # runs in Phase 4. Both paths share `_FACT_CHECK_CACHE`, so
    # validating here makes the post-review fact-check effectively free
    # for symbols already seen.
    fact_text = _strip_comparison_section(draft)
    claimed_structs = _extract_struct_names(fact_text)
    claimed_funcs = _extract_function_names(fact_text)
    extra_dirs = chapter.get("extra_search_dirs")
    missing_structs = set(_verify_structs(
        claimed_structs, SRC_ROOT, extra_dirs))
    missing_funcs = set(_verify_functions(
        claimed_funcs, SRC_ROOT, extra_dirs))
    verified_structs = [s for s in claimed_structs if s not in missing_structs]
    verified_funcs = [f for f in claimed_funcs if f not in missing_funcs]

    symbol_block = ""
    if verified_structs or verified_funcs or missing_structs or missing_funcs:
        symbol_block = (
            "## Verified Symbols (DO NOT hedge about these)\n\n"
            "The following struct and function names from the draft have\n"
            "been grep-checked against the FreeBSD source tree. Use this\n"
            "as ground truth for the Accuracy criterion: do NOT raise\n"
            "issues asking the writer to 'verify' a symbol on this list,\n"
            "and do NOT suggest alternate names. If a symbol you doubt\n"
            "is on the verified list, treat it as correct and move on.\n\n"
        )
        if verified_structs:
            symbol_block += (
                "Verified structs:\n"
                + "\n".join(f"- `struct {s}`" for s in sorted(verified_structs))
                + "\n\n"
            )
        if verified_funcs:
            symbol_block += (
                "Verified functions:\n"
                + "\n".join(f"- `{f}()`" for f in sorted(verified_funcs))
                + "\n\n"
            )
        if missing_structs:
            symbol_block += (
                "Missing structs (FAIL the Accuracy criterion if cited as\n"
                "if real — these names did not match any `struct NAME {`\n"
                "definition in the source tree):\n"
                + "\n".join(f"- `struct {s}` (NOT FOUND)"
                            for s in sorted(missing_structs))
                + "\n\n"
            )
        if missing_funcs:
            symbol_block += (
                "Missing functions (FAIL the Accuracy criterion if cited\n"
                "as if real — no matching definition found):\n"
                + "\n".join(f"- `{f}()` (NOT FOUND)"
                            for f in sorted(missing_funcs))
                + "\n\n"
            )

    # Undefined-jargon block. Computed here from the draft rather than
    # threaded in from `fact_check_draft`, matching how this builder already
    # derives its path/symbol verification: the scan needs no source tree
    # and is pure string work, so recomputing is cheaper than changing the
    # signature and every call site.
    #
    # This exists because criterion 8 ("Rationale") passed ch12 while it
    # used `KVM` five times without defining it. Asking the model to
    # simulate a junior reader does not work; handing it an explicit list
    # does. Phrased as a checklist the reviewer must account for, not as
    # advice it can weigh.
    unglossed = _extract_unglossed_jargon(draft)
    unexpanded = _extract_unexpanded_acronyms(draft)
    jargon_block = ""
    if unglossed or unexpanded:
        jargon_block = (
            "## Undefined Terms Detected (mechanical scan — trust this list)\n\n"
            "A deterministic scan found the following terms used in this\n"
            "draft's prose with no definition nearby. The scan masks fenced\n"
            "code blocks, so every term below is used in *prose* that a\n"
            "junior reader has to parse. These are NOT hallucinations — the\n"
            "terms are real and correctly used. The problem is that the\n"
            "draft never says what they mean.\n\n"
        )
        if unglossed:
            jargon_block += (
                "Jargon used without a gloss on first use:\n"
                + "\n".join(f"- `{t}`" for t in unglossed)
                + "\n\n"
            )
        if unexpanded:
            jargon_block += (
                "Acronyms used repeatedly but never expanded:\n"
                + "\n".join(f"- `{a}`" for a in unexpanded)
                + "\n\n"
            )
        jargon_block += (
            "For the Accessibility criterion (5) you MUST account for every\n"
            "term listed above: either FAIL the criterion and name the terms\n"
            "in `issues`, or explain per term why a gloss is genuinely\n"
            "unnecessary here. Do not pass Accessibility while silently\n"
            "ignoring this list.\n\n"
            "A gloss is one clause on first use — `KVM (the kernel's virtual\n"
            "address space)` — not a paragraph. Where the term names a\n"
            "*choice* the kernel offers (two modes, two flags, two\n"
            "allocators), the gloss must also say WHY both exist and when a\n"
            "caller picks each; that is criterion 8, and a definition alone\n"
            "does not satisfy it.\n\n"
        )

    # Pre-validate kernel-config options, DTrace SDT probes, and
    # MALLOC_DEFINE tags the same way as structs/functions. Same
    # rationale: the reviewer can't grep, so it either hedges (waste)
    # or invents wrongness (worse). The verifiers below run against
    # `sys/conf/options*` / `SDT_PROBE_DEFINE*` / `MALLOC_DEFINE*`
    # respectively and are cheap because the corpora are small (a few
    # hundred entries each); options + SDT do not share `_FACT_CHECK_CACHE`
    # because their verifiers don't go through `_verify_with_cache`,
    # but MALLOC tags do, so a tag verified here is free for fact-check.
    claimed_options = _extract_kernel_options(fact_text)
    claimed_probes = _extract_dtrace_probes(fact_text)
    claimed_mallocs = _extract_malloc_tags(fact_text)
    missing_options = set(_verify_kernel_options(claimed_options, SRC_ROOT))
    missing_probes_full = set(_verify_dtrace_probes(claimed_probes, SRC_ROOT))
    # `_verify_dtrace_probes` returns "provider:::name" strings; we
    # need to know which (provider, name) tuples are missing, so map back.
    missing_probes = {
        (p, n) for (p, n) in claimed_probes
        if f"{p}:::{n}" in missing_probes_full
    }
    missing_mallocs = set(_verify_malloc_tags(claimed_mallocs, SRC_ROOT))
    verified_options = [o for o in claimed_options if o not in missing_options]
    verified_probes = [(p, n) for (p, n) in claimed_probes
                       if (p, n) not in missing_probes]
    verified_mallocs = [m for m in claimed_mallocs if m not in missing_mallocs]

    macro_block = ""
    if (verified_options or missing_options or
            verified_probes or missing_probes or
            verified_mallocs or missing_mallocs):
        macro_block = (
            "## Verified Macros & Tags (DO NOT hedge about these)\n\n"
            "Kernel-config options, DTrace SDT probes, and MALLOC_DEFINE\n"
            "tags from the draft have been grep-checked against the\n"
            "FreeBSD source tree. Treat the verified lists as ground truth\n"
            "for the Accuracy criterion: do NOT ask the writer to 'verify'\n"
            "items on these lists, and do NOT propose alternate names.\n\n"
        )
        if verified_options:
            macro_block += (
                "Verified kernel-config options:\n"
                + "\n".join(f"- `option {o}`" for o in sorted(verified_options))
                + "\n\n"
            )
        if missing_options:
            macro_block += (
                "Missing kernel-config options (FAIL Accuracy if cited as\n"
                "real — no match in `sys/conf/options*` or `sys/conf/NOTES`):\n"
                + "\n".join(f"- `{o}` (NOT FOUND)"
                            for o in sorted(missing_options))
                + "\n\n"
            )
        if verified_probes:
            macro_block += (
                "Verified DTrace SDT probes:\n"
                + "\n".join(f"- `{p}:::{n}`"
                            for (p, n) in sorted(verified_probes))
                + "\n\n"
            )
        if missing_probes:
            macro_block += (
                "Missing DTrace SDT probes (FAIL Accuracy if cited as real\n"
                "— no matching `SDT_PROBE_DEFINE*` macro in sys/):\n"
                + "\n".join(f"- `{p}:::{n}` (NOT FOUND)"
                            for (p, n) in sorted(missing_probes))
                + "\n\n"
            )
        if verified_mallocs:
            macro_block += (
                "Verified MALLOC_DEFINE tags:\n"
                + "\n".join(f"- `{m}`" for m in sorted(verified_mallocs))
                + "\n\n"
            )
        if missing_mallocs:
            macro_block += (
                "Missing MALLOC_DEFINE tags (FAIL Accuracy if cited as real\n"
                "— no matching `MALLOC_DEFINE`/`MALLOC_DECLARE` in sys/):\n"
                + "\n".join(f"- `{m}` (NOT FOUND)"
                            for m in sorted(missing_mallocs))
                + "\n\n"
            )

    # Build the structure-rubric checklist from the chapter's section list.
    # If the chapter opted out of (e.g.) `Key Data Structures`, the reviewer
    # must NOT flag it as missing — that would always FAIL the structure
    # criterion for chapters that legitimately don't need every section.
    sections = _chapter_sections(chapter)
    section_count = len(sections) + 1  # +1 for the H1 title line
    structure_lines = [
        f"           - `## {name}` — {_SECTION_CATALOG[name]['rubric_body']}"
        for name in sections
    ]
    structure_checklist = "\n".join(structure_lines)

    # Mermaid criterion is conditional: chapters that omit "Flow / Diagram"
    # from their section list don't need a diagram, and grading them on one
    # would fail every revision round.
    wants_diagram = "Flow / Diagram" in sections
    if wants_diagram:
        mermaid_criterion = (
            f"        4. **Mermaid Diagram** — Is there a valid Mermaid {diagram} diagram?\n"
            f"           Check syntax: correct keywords, no missing brackets, proper arrows.\n"
            f"           Does it actually illustrate the subsystem (not a generic placeholder)?\n"
        )
        mermaid_json_line = '            "mermaid_diagram": "PASS/FAIL: reason",\n'
    else:
        mermaid_criterion = (
            "        4. **Mermaid Diagram** — N/A for this chapter (no Flow / Diagram\n"
            "           section). Always grade `mermaid_diagram` as `PASS: not required`.\n"
        )
        mermaid_json_line = '            "mermaid_diagram": "PASS: not required",\n'

    return textwrap.dedent(f"""\
        You are reviewing a draft chapter for "FreeBSD Internals."
        Your job is to find problems — be strict but fair.

        ## Chapter: {chapter['title']}

        ## Key Questions That Must Be Answered
        {question_text}

        ## Expected Source Files Referenced
        {chr(10).join(f"- {f}" for f in src_files)}

        {verified_block}
        {symbol_block}
        {macro_block}
        {jargon_block}
        {scope_guard_block}
        ## How You Operate (Sandbox Rules)

        You are running in a constrained Python sandbox. You have NO
        file-I/O tools, NO source-tree tools, NO subprocess, NO network.
        The draft is already in this prompt — it's the only material
        you need to grade. Specifically, do NOT attempt:
        - `open()`, `read()`, `write()`, `Path.read_text()`, etc. —
          all raise `InterpreterError: Forbidden function evaluation`
          and burn a step.
        - `os.path.exists`, `os.listdir`, `glob`, `subprocess.run` —
          same, all forbidden.
        - "Let me check the source to verify…" — you can't. The
          Verified blocks above ARE the source-tree ground truth for
          this review.
        Your only authorized imports are `json` and `re`. Use them
        only if you need to parse or pattern-match within the draft
        text in your input. Most reviews need no code at all — just
        emit the JSON verdict via `final_answer(...)` directly.

        ## Review Rubric

        Grade each criterion PASS / FAIL with a brief explanation:

        1. **Completeness** — Are ALL key questions above answered in the draft?
           Not hinted at — actually answered with technical detail.

        2. **Accuracy** — Does the draft reference real FreeBSD concepts correctly?
           No invented structs, no made-up function names, no wrong file paths.
           Flag anything that looks like a hallucination.
           IMPORTANT: you do NOT have direct access to the source tree.
           You CANNOT verify whether an arbitrary file path, struct,
           function, kernel-config option, DTrace SDT probe, or
           MALLOC_DEFINE tag exists. Do not claim that a name "does not
           exist in the FreeBSD source tree" unless it appears in a
           "missing" list above — names in the "Verified Source Paths",
           "Verified Symbols", or "Verified Macros & Tags" lists are
           confirmed real. For names in neither list, focus on whether
           their *use* in the draft is consistent (right subsystem,
           right relationships), not on whether they exist. FAIL
           Accuracy when an item is in any missing list AND the draft
           cites it as a real FreeBSD entity; PASS Accuracy when every
           cited symbol/option/probe/tag is either verified or
           unverifiable (not in any list).

        3. **Source Coverage** — Are the expected source files examined and
           discussed? Not just listed — actually explained with code snippets.

{mermaid_criterion}
        5. **Accessibility** — Is the tone educational? Does it explain
           WHY things work, not just WHAT they do? Are there analogies or
           connections to OS theory?
           Also: is every piece of jargon defined on first use? If an
           "Undefined Terms Detected" section appears above, every term in
           it must be accounted for here — FAIL and name them, or justify
           each omission. Judge as a competent C programmer with no kernel
           experience would: a term you personally recognize still needs a
           gloss for that reader.

        6. **Structure** — Does the draft have ALL {section_count} required
           sections with substantive content? Check for each:
{structure_checklist}
           FAIL if ANY section is missing, empty, or a single sentence.
           Sections OUTSIDE this list are not required (and not forbidden) —
           do not flag missing sections that are not on this list.

        7. **No marketing language** — FAIL if the draft contains any of
           these words or phrases (or close paraphrases):
           {_FORBIDDEN_MARKETING_WORDS}. ("advanced" as a generic adjective
           only; the section title "Advanced Notes" is allowed.) Quote the
           offending sentence(s) in the issue text so the writer can find
           and remove them.

        8. **Rationale** — When the draft introduces a non-obvious data
           structure or mechanism (shadow chains, inactive queues, UMA
           kegs vs zones, copy-on-write, slab caches, witness, turnstiles,
           NUMA domains, pagedaemon thresholds, etc.), does it explain
           WHY the design exists — what engineering problem it solves —
           and not just what it looks like? PASS if every non-obvious
           mechanism gets at least one sentence of rationale on its first
           introduction. FAIL if a major mechanism is described purely
           structurally ("X is a linked list of Y") with no reason for
           the design choice. Quote the offending paragraph(s) in the
           issue text so the writer can find and expand them. Trivial /
           obvious structures (a struct that just groups related fields,
           a red-black tree where O(log n) is self-evident) do NOT need
           rationale — apply this only where a junior reader would
           reasonably ask "but why is it built that way?"

        ## Draft to Review

        {draft}

        ## Your Output

        Output a JSON object with this structure — nothing else:

        {{
          "grade": "PASS" or "NEEDS_REVISION",
          "criteria": {{
            "completeness": "PASS/FAIL: reason",
            "accuracy": "PASS/FAIL: reason",
            "source_coverage": "PASS/FAIL: reason",
{mermaid_json_line}            "accessibility": "PASS/FAIL: reason",
            "structure": "PASS/FAIL: reason",
            "no_marketing": "PASS/FAIL: reason (quote any offending sentences)",
            "rationale": "PASS/FAIL: reason (quote the paragraph if FAIL)"
          }},
          "issues": [
            "Specific issue 1 with actionable fix",
            "Specific issue 2 with actionable fix"
          ],
          "praise": [
            "What works well — keep this in the revision"
          ]
        }}

        Grading rule (NO exceptions):
          - If ANY criterion is FAIL → `grade` MUST be "NEEDS_REVISION",
            and the FAILing criterion MUST have a corresponding entry in
            `issues` describing what to fix.
          - If every criterion is PASS, `grade` is "PASS". You MAY still
            list nice-to-have refinements in `issues`, but they are
            informational only and will not trigger another revision —
            so reserve them for genuinely worthwhile polish, not nitpicks.
          - Never mark a criterion PASS while describing a hallucination,
            wrong fact, or missing required content — that is what FAIL
            is for. Honest FAIL grades are far more useful than padded
            PASS grades.

        Be specific in issues: "The struct vm_page is described with fields that
        don't match sys/vm/vm_page.h" not "the data structures section is weak."

        **No hedges.** You do NOT have a source-tree tool of your own.
        Your ground truth is the "Verified Source Paths", "Verified
        Symbols", and "Verified Macros & Tags" blocks above (plus their
        "missing" counterparts). If a path / symbol / option / probe /
        tag is on neither list, that is a NON-FINDING — do NOT raise
        it. Forbidden issue shapes (omit them entirely):
        - "Verify that <symbol> is the correct name — FreeBSD may use
          <other_symbol> instead."
        - "Confirm against actual <file>" / "double-check against the
          source." You have no way to confirm; the writer will guess
          whichever name you suggested and may introduce a real bug.
        - "<name> may not exist" / "<name> might be misspelled" for any
          name in the Verified Source Paths, Verified Symbols, or
          Verified Macros & Tags lists.
        Raise an issue ONLY when (a) a path / symbol / option / probe /
        tag appears in a "missing" list above and the draft cites it as
        real, or (b) you can point to something INSIDE the draft that
        contradicts another part of the draft. The writer should be
        able to action every issue without guessing.
    """).lstrip()


def build_revision_prompt(chapter: dict, draft: str, review: str) -> str:
    """Build the revision prompt for the writer agent to fix issues."""
    return textwrap.dedent(f"""\
        You are revising an existing chapter for "FreeBSD Internals."
        A reviewer found specific issues — fix ONLY those, leave the
        rest of the draft untouched.

        ## Chapter: {chapter['title']}

        ## Review Feedback

        {review}

        ## Current Draft

        {draft}

        ## Your Task — PATCH MODE, NOT REWRITE MODE

        Treat this like a code-review patch: minimal, targeted edits.

        1. Address EVERY issue the reviewer listed — but ONLY those issues.
           Do NOT rewrite paragraphs the reviewer didn't flag, even if you
           think you can phrase them better.
        2. Do NOT re-explore the source tree. The draft below is already
           grounded in source. Only call `read_freebsd_source` /
           `resolve_c_definition` when the reviewer flagged a SPECIFIC
           hallucinated struct field, function signature, macro name, or
           file path that you must verify. One-or-two targeted lookups,
           not a fresh exploration.
        3. Keep everything the reviewer praised — copy those sentences
           through verbatim.
        4. If the reviewer flagged a hallucinated symbol, replace it with
           the verified real one in-place. Do not delete the surrounding
           paragraph unless the whole point was wrong.
        5. If the Mermaid diagram was flagged, fix only the broken edge
           or label. Do not redraw the whole diagram.

        ## Step Budget

        You have a hard step limit. Each tool call costs a step, and if
        you run out before emitting the corrected chapter via
        `final_answer(...)`, your work is discarded and the unrevised
        draft is kept. A typical revision should need 0–3 source-tree
        lookups. Spend your steps on writing, not on browsing.

        ## Output

        Output ONLY the complete corrected Markdown via `final_answer(...)`.
        No preamble, no explanation of changes, no diff. The reader should
        not see the review process, only the final polished chapter.
        IMPORTANT: emit the FULL chapter text — every section, including
        the parts you didn't change. Truncating the chapter is worse than
        leaving the original issues in.
    """).lstrip()


# ---------------------------------------------------------------------------
# 5. Agent Factory
# ---------------------------------------------------------------------------


# --- Per-chapter tool-use stats --------------------------------------------
#
# `source_stats.py` exists as a post-hoc log scanner: it greps tool-call
# lines out of /tmp/*.log and rolls them up by tool, file, and chapter.
# That works but has two real limitations:
#   - it can only see what the printer emitted (so revisions or stages
#     that share a chapter blur together), and
#   - the "per chapter" axis comes from a `Generating chapter N:` line
#     in the log, which is fragile across queue restarts.
#
# Embedding the same accounting inside `run_chapter` keeps it accurate
# (memory is the source of truth, not log text), keeps it per-stage
# (draft / review-N / revision-N / fact-fix is preserved), and
# eventually lets `source_stats.py` retire — though it stays useful for
# scanning logs from old runs that pre-date this banner.

# Match `tool(arg='value')` or `tool(arg="value")` with whitespace
# tolerance. These mirror the regexes in source_stats.py — keep them in
# sync if a writer tool gets added or renamed.
_STATS_TOOL_PATTERNS = [
    ("read_freebsd_source",
     re.compile(r"""read_freebsd_source\(\s*path\s*=\s*['"]([^'"]+)['"]""")),
    ("search_books",
     re.compile(r"""search_books\(\s*query\s*=\s*['"]([^'"]+)['"]""")),
    ("directory_map",
     re.compile(r"""directory_map\(\s*path\s*=\s*['"]([^'"]+)['"]""")),
    ("explore_tree",
     re.compile(r"""explore_tree\(\s*path\s*=\s*['"]([^'"]+)['"]""")),
    ("resolve_c_definition",
     re.compile(r"""resolve_c_definition\(\s*symbol\s*=\s*['"]([^'"]+)['"]""")),
]


def _collect_tool_stats(agent) -> dict:
    """Walk agent.memory.steps and tally tool calls in each `code_action`.

    Returns {
        'tool_counts': {tool_name: int},
        'reads': [path, path, ...],
        'maps': [path, ...],
        'resolves': [symbol, ...],
        'searches': [query, ...],
    }

    `agent.run(reset=True)` (the default) wipes memory at the start of
    each run, so calling this helper right after a `_run_agent(...)`
    captures only *that* stage's calls. Caller is responsible for
    snapshotting per stage and merging.
    """
    stats = {
        "tool_counts": {},
        "reads": [],
        "maps": [],
        "resolves": [],
        "searches": [],
    }
    mem = getattr(agent, "memory", None)
    steps = getattr(mem, "steps", None) if mem is not None else None
    if not isinstance(steps, list):
        return stats
    for step in steps:
        code = getattr(step, "code_action", None)
        if not isinstance(code, str) or not code:
            continue
        for tool, pat in _STATS_TOOL_PATTERNS:
            for m in pat.finditer(code):
                stats["tool_counts"][tool] = stats["tool_counts"].get(tool, 0) + 1
                arg = m.group(1)
                if tool == "read_freebsd_source":
                    stats["reads"].append(arg)
                elif tool == "directory_map":
                    stats["maps"].append(arg)
                elif tool == "resolve_c_definition":
                    stats["resolves"].append(arg)
                elif tool == "search_books":
                    stats["searches"].append(arg[:80])
    return stats


def _merge_tool_stats(dst: dict, src: dict, stage: str) -> None:
    """Merge a single-stage `_collect_tool_stats` result into the chapter accumulator.

    `dst` is the chapter-level accumulator that `run_chapter` carries
    across the whole pipeline. It mirrors the shape of the per-stage
    dict but with one extra key (`per_stage`) recording the tool count
    contributed by each labelled stage so the banner can show
    distribution (draft vs review vs fact-fix).
    """
    if "per_stage" not in dst:
        dst.update({
            "tool_counts": {},
            "reads": [],
            "maps": [],
            "resolves": [],
            "searches": [],
            "per_stage": {},
        })
    stage_total = 0
    for k, v in src["tool_counts"].items():
        dst["tool_counts"][k] = dst["tool_counts"].get(k, 0) + v
        stage_total += v
    for key in ("reads", "maps", "resolves", "searches"):
        dst[key].extend(src[key])
    if stage_total:
        dst["per_stage"][stage] = dst["per_stage"].get(stage, 0) + stage_total


def _format_stats_banner(stats: dict) -> str:
    """Render the chapter accumulator as a printable banner.

    The banner deliberately echoes `source_stats.py`'s output shape so
    that scanning either source feels familiar. Truncated to the most
    informative slices: per-tool totals, per-stage totals, top reads,
    top resolved symbols.
    """
    if not stats.get("per_stage"):
        return ""
    lines = ["  ──── tool-use summary ────"]
    total = sum(stats["tool_counts"].values())
    lines.append(f"  total tool calls: {total}")
    if stats["per_stage"]:
        per_stage = ", ".join(
            f"{stage}={n}" for stage, n in stats["per_stage"].items()
        )
        lines.append(f"  per stage: {per_stage}")
    if stats["tool_counts"]:
        per_tool = ", ".join(
            f"{tool}={n}" for tool, n in
            sorted(stats["tool_counts"].items(), key=lambda x: -x[1])
        )
        lines.append(f"  per tool: {per_tool}")
    # Top-N file reads — duplicates collapsed.
    if stats["reads"]:
        from collections import Counter
        top_reads = Counter(stats["reads"]).most_common(5)
        if top_reads:
            preview = ", ".join(f"{p}×{n}" for p, n in top_reads)
            lines.append(f"  top reads: {preview}")
    if stats["resolves"]:
        from collections import Counter
        top_res = Counter(stats["resolves"]).most_common(5)
        if top_res:
            preview = ", ".join(f"{s}×{n}" for s, n in top_res)
            lines.append(f"  top resolves: {preview}")
    return "\n".join(lines)


def _agent_step_count(agent) -> Optional[int]:
    """Best-effort: return the number of steps the agent took on its last run.

    smolagents has reshuffled this attribute across versions. We try the
    known names in order and fall back to None silently — this is only used
    for diagnostic logging, never for control flow.
    """
    # Newer smolagents: memory.steps is a list of step records
    mem = getattr(agent, "memory", None)
    steps = getattr(mem, "steps", None) if mem is not None else None
    if isinstance(steps, list):
        return len(steps)
    # Older smolagents: a flat step_count counter
    sc = getattr(agent, "step_count", None)
    if isinstance(sc, int):
        return sc
    # Older still: logs/step_log
    sl = getattr(agent, "step_log", None) or getattr(agent, "logs", None)
    if isinstance(sl, list):
        return len(sl)
    return None


def _looks_like_stub(text: str) -> bool:
    """Detect a degenerate writer output captured at max_steps exhaustion.

    When the writer agent runs out of steps mid-revision, smolagents
    surfaces the *last* step output instead of a `final_answer()` payload.
    For a CodeAgent that's typically a tool-call snippet (`<code>...</code>`,
    `Calling tools:`, `'function':`, `'arguments':`) or a few lines of
    Python — never a full chapter. Writing that on top of a previously-good
    draft destroys the chapter (we've seen 15- and 26-line stubs replace
    1000+ line drafts).

    Treat as a stub when the text is short AND lacks any of the expected
    chapter H2 headers. Either condition alone is too aggressive: a short
    chapter is plausible, and an agent reading source code can produce a
    long log without writing prose.
    """
    if not text:
        return True
    body = text.strip()
    if len(body) < 600:
        return True
    has_h2 = bool(re.search(r'^[ ]{0,3}##\s+\S', body, re.MULTILINE))
    has_call_artifact = bool(
        re.search(r'(?:^|\n)Calling tools:\b', body)
        or re.search(r"'function'\s*:\s*\{", body)
        or re.search(r'(?:^|\n)<code>\s*\n', body)
    )
    if not has_h2 and has_call_artifact:
        return True
    if not has_h2 and len(body.splitlines()) < 50:
        return True
    return False


def _finish_reason(step) -> Optional[str]:
    """Pull the API finish_reason out of one smolagents memory step."""
    msg = getattr(step, "model_output_message", None)
    raw = getattr(msg, "raw", None) if msg is not None else None
    choices = getattr(raw, "choices", None)
    if not choices:
        # Some backends hand back a plain dict rather than an SDK object.
        if isinstance(raw, dict):
            try:
                return raw["choices"][0].get("finish_reason")
            except (KeyError, IndexError, AttributeError, TypeError):
                return None
        return None
    try:
        return getattr(choices[0], "finish_reason", None)
    except (IndexError, TypeError):
        return None


def _warn_on_token_truncation(agent, label: str) -> None:
    """Warn when a step was cut off by WRITER_MAX_TOKENS.

    Truncation is otherwise SILENT: the server returns
    `finish_reason: "length"`, smolagents drops it, and the pipeline just
    sees a short response. If the cap is set too low for a chapter, every
    draft truncates, best-draft tracking dutifully ships the least-bad
    partial, and nothing in the log looks wrong — a quiet quality
    regression, which is the worst kind. Say so instead.

    Best-effort by design: `finish_reason` plumbing varies by backend, so
    anything unreadable is skipped rather than guessed at.
    """
    if WRITER_MAX_TOKENS <= 0:
        return
    steps = getattr(getattr(agent, "memory", None), "steps", None) or []
    hits = sum(1 for s in steps if _finish_reason(s) == "length")
    if not hits:
        return
    print(f"  ⚠ {label}: {hits} step(s) hit the {WRITER_MAX_TOKENS}-token "
          f"generation cap (finish_reason=length) — output was cut mid-"
          f"stream.")
    print(f"     If this repeats, the chapter needs more room: raise "
          f"DAEMONDOCS_WRITER_MAX_TOKENS (0 disables the cap).")


def _run_agent(agent, label: str, prompt: str,
               stats: Optional[dict] = None) -> str:
    """Run an agent and warn if it hit its step cap.

    Hitting the cap usually means the model ran out of room to produce
    well-formed output (truncated JSON, missing sections). Surfacing it in
    the run log is the cheapest way to diagnose silent quality regressions.

    smolagents' `final_answer()` returns the raw object the agent passed
    in — which can be a dict, list, or other non-string. Every caller here
    expects a string (they call `.strip()`, write to disk, or feed into
    JSON-extraction). Coerce at this boundary so callers never have to.

    `stats` (optional) is the chapter-level tool-use accumulator. When
    supplied, the per-stage tool calls extracted from `agent.memory.steps`
    are merged in under `label`. Memory is reset on the next `agent.run()`,
    so collection happens here while it's still fresh.
    """
    # Name the phase for the hang detector. Without this every agent stage
    # runs under the stale label from whatever ran last ("startup" on the
    # initial draft), so a dump misattributes the stall. agent.run() is a
    # single blocking call — smolagents drives its own step loop inside —
    # so there is no inner point to beat() from; the label is what makes
    # the dump readable, and the long quiet timeout is what keeps normal
    # multi-minute LLM latency from tripping it.
    with heartbeat(f"agent:{label}"):
        result = agent.run(prompt)
    cap = getattr(agent, "max_steps", None)
    used = _agent_step_count(agent)
    if isinstance(cap, int) and isinstance(used, int) and used >= cap:
        print(f"  ⚠ {label}: hit max_steps={cap} — output may be truncated")
    _warn_on_token_truncation(agent, label)
    if stats is not None:
        _merge_tool_stats(stats, _collect_tool_stats(agent), label)
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    # Dict/list → serialise as JSON so _extract_json can still parse it.
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, indent=2)
        except (TypeError, ValueError):
            return str(result)
    return str(result)


def create_writer_agent(index: TfidfIndex):
    """Create the writer CodeAgent — reads source, searches books, writes docs."""
    # Pin sampler params at the call site so primary and secondary
    # llama-server endpoints behave identically. We've observed at least
    # one drift mode (chapter 7 returning a status string instead of the
    # chapter) on the secondary endpoint that did not reproduce on the
    # primary, traceable to differing /props defaults. These values match
    # the model's recommended sampling for instruction-following.
    # Per-call generation ceiling — see WRITER_MAX_TOKENS in Configuration
    # for the measurement. Passed as a dict so 0 means "send no max_tokens
    # at all" (genuinely unlimited) rather than "max_tokens=0", which the
    # server would read as generate-nothing.
    cap = ({"max_tokens": WRITER_MAX_TOKENS}
           if WRITER_MAX_TOKENS > 0 else {})

    model = OpenAIServerModel(
        model_id=MODEL_CONFIG["model_id"],
        api_base=MODEL_CONFIG["api_base"],
        api_key=MODEL_CONFIG["api_key"],
        # client_kwargs is forwarded to openai.OpenAI(); `timeout` here
        # bounds the read timeout per HTTP call. See MODEL_CONFIG above.
        client_kwargs={"timeout": MODEL_CONFIG["timeout"]},
        temperature=0.6,
        top_p=0.95,
        **cap,
        # Reasoning channel, default ON — see WRITER_THINKING in the
        # Configuration banner for the two-arm measurement that settled
        # this. Short version: disabling it is ~3x faster PER CALL and
        # far more expensive PER CHAPTER, because the writer substitutes
        # tool calls for deliberation and stops converging. Per-call
        # latency is the wrong unit; total tokens to a finished chapter is
        # the right one.
        #
        # `reasoning_effort: "none"` is the SDK-native equivalent and gives
        # byte-identical output; `/no_think` in the prompt is IGNORED on
        # this build (still emitted 653 chars of reasoning).
        #
        # This instance is reused for draft / revise / fact-fix, so all
        # three writer stages share the setting. The reviewer is gated
        # separately — see create_reviewer_agent.
        #
        # MUST go through `extra_body`: the openai SDK validates keyword
        # arguments against Completions.create()'s signature and raises
        # TypeError on unknown ones, so passing chat_template_kwargs at the
        # top level fails every call before it reaches the wire.
        # `extra_body` is merged into the JSON request body unvalidated,
        # which is the only way to reach a llama.cpp-specific parameter.
        extra_body={"chat_template_kwargs": {"enable_thinking": WRITER_THINKING}},
    )

    return CodeAgent(
        tools=[
            ReadFreeBSDSource(),
            SearchBooks(index),
            ExploreTree(),
            DirectoryMap(),
            ResolveCDefinition(),
        ],
        model=model,
        # Deliberately exclude `os` and `pathlib`: the writer has no
        # legitimate need for filesystem access (its tools cover all
        # source reads), and authorizing them lets the model bypass the
        # "no file I/O" prompt rule by writing chapter content to
        # /tmp/corrected_chapter.md and returning a status string —
        # exactly the drift mode the prompt warns against. Keeping the
        # sandbox honest is more reliable than relying on the prompt.
        additional_authorized_imports=["re", "json"],
        # 40 was too low for wide-scope networking chapters: the IP Layer
        # chapter (10 source files across sys/netinet, sys/netinet6,
        # sys/net/route) hit max_steps in *both* the draft and fact-fix
        # phases on 2026-05-01, shipping with `⚠ UNVERIFIED DRAFT` and a
        # leaked `final_answer(...)` literal at the top of the output.
        # The mbuf chapter hit max_steps in draft *and* errored in
        # revision 1. Bumped to 80 — same agent instance is reused for
        # draft / revise / fact-fix, so wide chapters need headroom for
        # all three.
        max_steps=80,
        # Stream tokens as they arrive instead of awaiting the whole
        # response in one shot. Two reasons:
        #   1. Hang detection. 2026-05-03 ch13 wedged on fw2: llama-server
        #      finished generating, the response (or trailing chunk) was
        #      lost on the wire, and the non-streaming `chat.completions.
        #      create(...)` call sat in httpx read forever — single-float
        #      `timeout=600s` reset on every byte received and never tripped.
        #      Streaming raises an httpx ReadTimeout if no chunk arrives for
        #      `read` seconds, surfacing the hang as APITimeoutError that
        #      the existing run_chapter except clause catches.
        #   2. Live progress in the per-chapter log: each token shows up
        #      immediately, so `tail -f /tmp/regen-queue/<lbl>-ch<N>.log`
        #      shows real activity instead of multi-minute silences.
        stream_outputs=True,
    )


def create_reviewer_agent(index: TfidfIndex):
    """Create the reviewer agent — critiques drafts, no source tools needed."""
    # Same pinning as the writer — see create_writer_agent for rationale.
    #
    # Reasoning is gated by REVIEWER_THINKING (env DAEMONDOCS_REVIEWER_THINKING)
    # so the two llama-server endpoints can run opposite settings against the
    # shared chapter queue and be compared directly. Unlike the writer — where
    # thinking is always off, because transcription doesn't benefit from it and
    # the extra headroom invites fabrication — the reviewer's value here is
    # genuinely unmeasured. See the REVIEWER_THINKING comment in Configuration.
    model = OpenAIServerModel(
        model_id=MODEL_CONFIG["model_id"],
        api_base=MODEL_CONFIG["api_base"],
        api_key=MODEL_CONFIG["api_key"],
        client_kwargs={"timeout": MODEL_CONFIG["timeout"]},
        temperature=0.6,
        top_p=0.95,
        # via extra_body — see create_writer_agent for why the top-level
        # kwarg does not work with the openai SDK.
        extra_body={"chat_template_kwargs": {"enable_thinking": REVIEWER_THINKING}},
    )

    return CodeAgent(
        tools=[
            SearchBooks(index),
        ],
        model=model,
        additional_authorized_imports=["json", "re"],
        # 15 was generous and turned into a foot-gun: ch11 review pass 3
        # on 2026-05-03 burned 1h+ of fw on a single step that produced
        # 121K tokens of thinking with no parsed code, then ran into
        # repeated InterpreterError on forbidden open() calls (the
        # reviewer has no file I/O — see the prompt guard added in the
        # same change). A reviewer that hasn't issued a verdict by step
        # 5 is going to ship UNVERIFIED regardless; capping early frees
        # the queue faster on bad chapters.
        max_steps=5,
        # See create_writer_agent for the rationale — streams tokens so
        # an httpx read-gap raises ReadTimeout instead of wedging the run.
        stream_outputs=True,
    )


# ---------------------------------------------------------------------------
# 6. Orchestrator
# ---------------------------------------------------------------------------


def load_chapters() -> List[dict]:
    """Load chapter definitions from chapters.yaml."""
    if not CHAPTERS_FILE.exists():
        print(f"Error: {CHAPTERS_FILE} not found")
        sys.exit(1)
    with open(CHAPTERS_FILE) as f:
        data = yaml.safe_load(f)
    return data.get("chapters", [])


# Canonical list of reviewer criteria — the single source of truth for the
# criterion COUNT used in every "N/total pass" print and the best-draft
# fail-count bookkeeping. These are the keys the reviewer is instructed to
# emit in build_review_prompt's JSON schema; mermaid_diagram is always
# present (graded when a diagram is wanted, "PASS: not required" otherwise).
# Deriving the count from here means adding/removing a criterion in the
# rubric can't silently desync the display math — previously the count `8`
# was hard-coded in ~6 places and CODE_MAP had to warn "bump manually."
_REVIEW_CRITERIA = (
    "completeness", "accuracy", "source_coverage", "mermaid_diagram",
    "accessibility", "structure", "no_marketing", "rationale",
)
_N_CRITERIA = len(_REVIEW_CRITERIA)


def _review_passes(review_json: Optional[dict]) -> bool:
    """Review gate: criteria are the canonical assessment, not `grade`/`issues`.

    History: an earlier version accepted on grade==PASS *or* empty issues,
    which silently approved drafts where individual criteria said FAIL.
    The over-correction was to also require empty issues — but that ran
    into a second failure mode where the reviewer marks all criteria
    PASS and still pads `issues` with stylistic nits, forcing another
    revision round (and risking regressions where the writer reintroduces
    bugs we already fixed).

    The current gate trusts the **criteria** dict as ground truth:
      - the reviewer returned parseable JSON,
      - `criteria` is a dict and **every** value is a string that does not
        start with `"FAIL"` (so a missing/typo'd criterion doesn't sneak
        through and a real FAIL always blocks).

    `grade` and `issues` are intentionally NOT consulted here. The reviewer
    prompt now treats `issues` as informational when all criteria pass; if
    a real defect exists, it must surface as a FAIL criterion.
    """
    if not isinstance(review_json, dict):
        return False
    criteria = review_json.get("criteria")
    if not isinstance(criteria, dict) or not criteria:
        return False
    for v in criteria.values():
        if not isinstance(v, str):
            return False
        if v.startswith("FAIL"):
            return False
    return True


def _criteria_fail_count(criteria: object) -> int:
    """Count FAIL criteria, tolerating non-string / missing values.

    Originally this was a bare `v.startswith("FAIL")` which crashed on
    null or list values. Treat anything non-string as a failure (safer
    default for the summary line).
    """
    # Broken-JSON worst case: assume every criterion failed. The count
    # is derived from _REVIEW_CRITERIA so it tracks the rubric.
    if not isinstance(criteria, dict):
        return _N_CRITERIA
    fails = 0
    for v in criteria.values():
        if not isinstance(v, str) or v.startswith("FAIL"):
            fails += 1
    return fails


def run_chapter(chapter: dict, writer, reviewer, max_revisions: int,
                dry_run: bool = False) -> bool:
    """Run the multi-pass pipeline: draft → review/revise loop → fact-check → write.

    writer      — the writer CodeAgent
    reviewer    — the reviewer CodeAgent
    max_revisions — how many revision rounds to allow (0 = single pass, no review)

    --- Pipeline steps and the role of each agent ----------------------------

    Two agents, distinct roles. The reviewer NEVER edits the draft text — it
    only emits a JSON verdict. Every textual change (including additions) is
    produced by the writer in a follow-up call. This separation is why each
    step's prompt can be small and focused: the reviewer's prompt does not
    need to teach it how to write, and the writer's revise/fact-fix prompts
    do not need to re-derive the rubric.

    Step 1 — DRAFT (writer)
        Input:  build_chapter_prompt(chapter) — focus, scope_guard, sections,
                key questions, mandatory output template, no-marketing rule,
                and the existing target file (if any) as read-only context.
        Tools:  read_freebsd_source, search_books, explore_tree,
                directory_map, resolve_c_definition.
        Output: a full markdown draft. Free to add anything within the
                template; bound by scope_guard and forbidden-words list.

    Step 2 — REVIEW (reviewer, looped)  [skipped when max_revisions == 0]
        Input:  build_review_prompt(chapter, draft) — chapter scope/rubric +
                the current draft.
        Tools:  search_books only (no source-tree access — the rubric is
                evaluated against the draft itself, not against the tree).
        Output: a JSON verdict with grade, issues[], praise[], and per-
                criterion stamps (PASS / FAIL: <reason>). Does NOT modify
                the draft. JSON parse failures get one retry before the
                chapter is marked unapproved.
        Gate:   _review_passes — criteria-driven. Approves when every
                criterion in `criteria` starts with "PASS" (and the dict
                is well-formed). `grade` and `issues` are intentionally
                ignored: real defects must surface as FAIL criteria, and
                non-FAIL `issues` are informational (otherwise we waste
                revision rounds chasing stylistic nits and risk the writer
                regressing earlier fact fixes during the revision pass).

    Step 3 — REVISE (writer)  [only when review fails]
        Input:  build_revision_prompt(chapter, draft, review_raw) — original
                chapter prompt + previous draft + reviewer's raw JSON.
        Output: a new full draft. The writer can both REMOVE content (e.g.
                trim out-of-scope sections the reviewer flagged) AND ADD
                content (e.g. fill a section the reviewer marked thin, or
                answer a key question the draft missed). Loop returns to
                Step 2 with the new draft until approved or max_revisions
                exhausted.

    Step 4 — FACT-CHECK (deterministic, no agent)
        Input:  the approved/unapproved draft.
        Logic:  fact_check_draft — extracts every claimed file path, struct
                name, and function name from the markdown; verifies each
                against the FreeBSD source tree (the 3-stage grep pipeline
                in _batched_grep_present, plus _resolve_path_in_tree).
        Output: dict of {missing_paths, corrected_paths, missing_structs,
                missing_funcs}. Does NOT modify the draft.

    Step 5 — FACT-FIX (writer)  [only when fact-check finds issues]
        Input:  _build_fact_check_prompt(chapter, draft, facts) — original
                chapter context + current draft + the specific bad claims.
        Output: a new draft with bad paths corrected, missing structs
                replaced with verified ones, and any unfixable claims
                removed. Same writer agent as Step 1 — has full source
                access via tools, so it can find correct replacements.

    Step 6 — ATOMIC WRITE
        Input:  the final draft (post fact-fix), with an UNVERIFIED DRAFT
                banner inserted under the H1 if the reviewer didn't approve
                or if fact-fix crashed.
        Logic:  rename existing output to .bak, _atomic_write the new
                draft, delete .bak on success / restore .bak on failure.

    --- Reliability guarantees ----------------------------------------------
      - The previously-generated file (if any) is preserved on every failure
        path. We rename it to a `.bak` sibling and only delete the backup once
        a fresh, complete draft has been written successfully.
      - The final write is atomic — a Ctrl-C or crash during disk write leaves
        either the previous file (via the backup) or the new file, never a
        truncated mix.
      - If the chapter is approved but the fact-fix revision crashes, we mark
        the output as `unverified` rather than silently writing a draft we know
        contains hallucinated paths/structs.
    """
    title = chapter["title"]
    output_file = chapter.get("output_file", "README.md")
    output_path = os.path.join(SRC_ROOT, output_file)
    backup_path = output_path + ".freebsd-docs.bak"

    print(f"\n{'=' * 70}")
    print(f"  Chapter: {title}")
    print(f"  Output:  {output_path}")
    print(f"  Revisions: up to {max_revisions}")
    # Logged (not written into the chapter footer, which is public-facing)
    # so a finished run can be split by reviewer-thinking arm after the fact.
    print(f"  Endpoint: {MODEL_CONFIG['api_base']}")
    print(f"  Writer thinking:   {'on' if WRITER_THINKING else 'off'}")
    print(f"  Writer max_tokens: "
          f"{WRITER_MAX_TOKENS if WRITER_MAX_TOKENS > 0 else 'unlimited'}")
    print(f"  Reviewer thinking: {'on' if REVIEWER_THINKING else 'off'}")
    print(f"{'=' * 70}")

    # Fail fast on an output_file whose parent directory does not exist in the
    # source tree. `output_file` is relative to SRC_ROOT, so a missing top-level
    # prefix still forms a valid-looking relative path and the atomic write
    # happily creates the whole layout: ch17 shipped to a brand-new top-level
    # `fs/nfs/` because its output_file read "fs/nfs/README.md" instead of
    # "sys/fs/nfs/README.md" (2026-08-27). The chapter CONTENT was fine —
    # source_dirs were correct — so nothing downstream had reason to complain,
    # and the README simply never landed in the tree it documents.
    #
    # Only pre-existing directories are legitimate targets: every chapter
    # documents code that is already there. Checked before the writer runs so a
    # typo costs seconds instead of a multi-hour chapter.
    if not dry_run:
        parent = os.path.dirname(output_path)
        if parent and not os.path.isdir(parent):
            rel_parent = os.path.dirname(output_file)
            print(f"  ✗ output_file '{output_file}' targets "
                  f"'{rel_parent}/', which does not exist under {SRC_ROOT}.")
            # A missing `sys/` prefix is by far the likeliest cause, so name it.
            if os.path.isdir(os.path.join(SRC_ROOT, "sys", rel_parent)):
                print(f"    Did you mean 'sys/{output_file}'? "
                      f"('sys/{rel_parent}/' does exist.)")
            print("    Refusing to create a new directory layout in the "
                  "source tree. Fix output_file in chapters.yaml.")
            return False

    if dry_run:
        print("  [dry-run] would generate README.md")
        return False

    # NOTE: We deliberately do NOT move the existing output aside up-front.
    # When `output_file` is something like `README.md` and the chapter's
    # `source_files` *also* lists `README.md`, renaming it before the writer
    # runs would prevent the writer from reading its own source. Instead we
    # back up just before the atomic write below, so the rename window is
    # narrow and never overlaps tool reads.
    had_backup = False

    # Per-chapter tool-use accumulator. Every `_run_agent` call below
    # passes this in, and we emit a banner just before returning. See
    # `_collect_tool_stats` / `_format_stats_banner` for the shape.
    stats: dict = {}

    success = False
    try:
        # ---- Pass 1: initial draft ----
        prompt = build_chapter_prompt(chapter)
        print("  [draft] writing initial chapter ...")

        try:
            draft = _run_agent(writer, "draft", prompt, stats=stats)
        except Exception as e:
            print(f"  ✗ initial draft failed: {e}")
            return False

        # Defensive: if the very first draft is a stub, retry once with a
        # pointed reminder. We've observed the writer drift into
        # "I'm a coding agent that writes files" mode — it builds the
        # content, hits the open() ban, and eventually returns a status
        # string ("README.md successfully written...") via final_answer
        # instead of the chapter. _looks_like_stub catches that. A single
        # retry with an even more explicit prompt tends to recover.
        if _looks_like_stub(draft):
            print("  ⚠ initial draft looks truncated/stub — retrying once "
                  "with explicit final_answer instructions ...")
            retry_prompt = (
                prompt
                + "\n\n---\n\n"
                + "**RETRY — your previous attempt was rejected as a stub.**\n\n"
                + "You likely returned a short status string (e.g.\n"
                + "'README.md successfully written...') or tried to write\n"
                + "the chapter to a file. NEITHER works.\n\n"
                + "Do this instead, exactly:\n"
                + "1. Build the complete Markdown content as a Python string\n"
                + "   variable (call it `content`).\n"
                + "2. Call `final_answer(content)`. The return value of\n"
                + "   final_answer IS the chapter — it must be the full\n"
                + "   Markdown body starting with `# " + chapter['title'] + "`.\n"
                + "3. Do NOT call open(), Path.write_text, or any file I/O.\n"
                + "   They are forbidden and will raise InterpreterError.\n"
                + "4. The pipeline writes the file once you return. Your job\n"
                + "   is to return the content string — nothing else.\n"
            )
            try:
                draft = _run_agent(writer, "draft-retry", retry_prompt,
                                   stats=stats)
            except Exception as e:
                print(f"  ✗ initial draft retry failed: {e}")
                return False
            if _looks_like_stub(draft):
                print("  ✗ initial draft retry still produced a stub — "
                      "aborting chapter")
                return False
            print("  ✓ retry produced a real draft, continuing")

        # ---- Review + revision loop ----
        # Strict gate: only stop when the reviewer truly approves. JSON parse
        # failures get one retry instead of being treated as approval.
        #
        # Best-draft tracking: revisions can regress. We've seen rounds go
        # 3/7 → 6/7 → 5/7, with the writer introducing new hallucinations
        # while patching old ones. Without this tracker we'd write the
        # last (worse) draft. Instead we keep the draft from the round
        # with the fewest FAIL criteria; ties go to the *later* round
        # (later drafts have had more issues addressed even if criteria
        # count is unchanged). best_fails starts at _N_CRITERIA + 1 —
        # strictly worse than any real review (max possible is
        # _N_CRITERIA) — so the very first graded draft always wins on
        # first comparison.
        warnings: List[str] = []
        revision = 0
        approved = False
        parse_retry_used = False
        best_draft = draft
        best_fails = _N_CRITERIA + 1
        best_round = 0
        last_fails: Optional[int] = None  # fail_count of the most recent graded round

        while max_revisions > 0 and revision < max_revisions:
            revision += 1
            print(f"  [review {revision}] evaluating draft ...")

            try:
                review_prompt = build_review_prompt(chapter, draft)
                review_raw = _run_agent(reviewer, f"review {revision}",
                                        review_prompt, stats=stats)
            except Exception as e:
                print(f"  ✗ review {revision} failed: {e}")
                break

            review_json = _extract_json(review_raw)
            if review_json is None:
                if not parse_retry_used:
                    parse_retry_used = True
                    print(f"  ⚠ review {revision}: could not parse JSON, retrying once")
                    revision -= 1   # don't consume a revision slot for a parse retry
                    continue
                print(f"  ⚠ review {revision}: JSON unparseable twice — "
                      "treating chapter as NOT approved")
                break

            grade = review_json.get("grade", "UNKNOWN")
            issues = review_json.get("issues", []) or []
            praise = review_json.get("praise", []) or []
            criteria = review_json.get("criteria", {}) or {}

            fail_count = _criteria_fail_count(criteria)
            print(f"         grade={grade}  "
                  f"({_N_CRITERIA - fail_count}/{_N_CRITERIA} criteria pass)")
            # Print every issue and every praise — truncating these hides
            # the diagnostic information needed to figure out why the
            # reviewer didn't approve. The log is verbose by design.
            if issues:
                for iss in issues:
                    print(f"         - {iss}")
            if praise:
                for p in praise:
                    print(f"         ✓ {p}")

            # Track the best draft we've seen so far (fewest FAIL criteria).
            # `<=` not `<`: ties go to the more recent round because later
            # drafts have had earlier issues addressed even when the FAIL
            # count is unchanged.
            last_fails = fail_count
            if fail_count <= best_fails:
                best_draft = draft
                best_fails = fail_count
                best_round = revision

            if _review_passes(review_json):
                print(f"  [review {revision}] chapter passes — no revision needed")
                approved = True
                break

            # ---- Revision pass ----
            print(f"  [revision {revision}] rewriting to address "
                  f"{len(issues) or fail_count} issue(s) ...")

            try:
                revision_prompt = build_revision_prompt(chapter, draft, review_raw)
                new_draft = _run_agent(writer, f"revision {revision}",
                                       revision_prompt, stats=stats)
            except Exception as e:
                print(f"  ✗ revision {revision} failed: {e}")
                break

            # If the revision came back as a tool-call stub (writer hit
            # max_steps before emitting a `final_answer()`), keep the
            # previous draft instead of writing a 15-line shred over a
            # 200-line chapter. The reviewer's complaints persist, but
            # the reader gets readable prose.
            if _looks_like_stub(new_draft):
                print(f"  ⚠ revision {revision}: output looks truncated/stub — "
                      f"keeping prior draft")
                warnings.append(
                    f"revision {revision} truncated — kept prior draft")
                # Don't continue revising on top of broken state.
                break
            draft = new_draft

        if max_revisions > 0 and not approved:
            print("  ⚠ review loop exited without explicit approval — "
                  "writing draft but flagging as unverified")
            # Roll back to the best draft we saw if revisions regressed.
            # We've observed rounds going 3/7 → 6/7 → 5/7 where the final
            # draft has more hallucinations than an earlier round.
            # Only roll back when the current draft is *strictly* worse
            # than the best one seen — equal-quality later rounds keep
            # the more recent draft (later prose tends to be cleaner).
            if (
                best_round > 0
                and best_round != revision
                and last_fails is not None
                and last_fails > best_fails
            ):
                print(f"  [rollback] revision {revision} regressed "
                      f"({_N_CRITERIA - last_fails}/{_N_CRITERIA}) — using "
                      f"revision {best_round} "
                      f"({_N_CRITERIA - best_fails}/{_N_CRITERIA}) instead")
                draft = best_draft
                warnings.append(
                    f"revisions regressed; kept revision {best_round} "
                    f"({_N_CRITERIA - best_fails}/{_N_CRITERIA} criteria) over "
                    f"revision {revision} "
                    f"({_N_CRITERIA - last_fails}/{_N_CRITERIA})"
                )

        # ---- Fact-checking pass ----
        print("  [fact-check] verifying paths, structs, funcs, options, "
              "dtrace probes ...")
        # Named phase so a hang-detector dump says "fact-check" rather
        # than just showing a stack. This is the phase that wedged for
        # 6.8 hours on 2026-08-22 with no output at all.
        with heartbeat(f"fact-check: {title}"):
            facts = fact_check_draft(
                draft, SRC_ROOT,
                extra_search_dirs=chapter.get("extra_search_dirs"),
            )
        fact_check_clean = facts['total_issues'] == 0
        fact_fix_failed = False

        if not fact_check_clean:
            print(f"         found {facts['total_issues']} issue(s):")
            if facts['file_paths_not_found']:
                print(f"         - missing paths: {', '.join(facts['file_paths_not_found'])}")
            if facts['file_paths_corrected']:
                for old, right in (x.split(' → ') for x in facts['file_paths_corrected']):
                    print(f"         - path correction: `{old}` → `{right}`")
            if facts['structs_not_found']:
                print(f"         - missing structs: {', '.join(facts['structs_not_found'])}")
            if facts['funcs_not_found']:
                print(f"         - missing functions: {', '.join(facts['funcs_not_found'])}")
            if facts.get('func_sigs_mismatch'):
                print(f"         - signature arity mismatches: "
                      f"{'; '.join(facts['func_sigs_mismatch'])}")
            if facts.get('kernel_options_not_found'):
                print(f"         - missing kernel options: "
                      f"{', '.join(facts['kernel_options_not_found'])}")
            if facts.get('dtrace_probes_not_found'):
                print(f"         - missing dtrace probes: "
                      f"{', '.join(facts['dtrace_probes_not_found'])}")

            print("  [fact-fix] rewriting to address fact-check issues ...")
            try:
                fact_prompt = _build_fact_check_prompt(chapter, draft, facts)
                new_draft = _run_agent(writer, "fact-fix", fact_prompt,
                                       stats=stats)
                if _looks_like_stub(new_draft):
                    # Hit max_steps before producing prose. Keep the
                    # pre-fact-fix draft (still contains hallucinations
                    # but is at least readable) and mark fact-fix failed.
                    print("  ⚠ fact-fix: output looks truncated/stub — "
                          "keeping pre-fact-fix draft")
                    fact_fix_failed = True
                else:
                    draft = new_draft
            except Exception as e:
                # The draft we have still contains the known hallucinations.
                # Mark it so a reader knows not to trust path/struct claims.
                print(f"  ✗ fact-check revision failed: {e}")
                fact_fix_failed = True
        else:
            print("         all claims verified — no issues found")

        # ---- Mermaid sanitizer ----
        # Catches subgraph/node id collisions that make Mermaid refuse
        # to render with a "would create a cycle" error. Idempotent and
        # cheap; runs unconditionally so a writer that ignores the
        # corresponding prompt rule still ships a renderable diagram.
        # See _sanitize_mermaid_flowchart for the post-mortem.
        sanitized = _sanitize_mermaid_blocks(draft)
        if sanitized != draft:
            print("  [mermaid] rewrote subgraph id(s) to break "
                  "node-id collision")
            draft = sanitized

        # ---- Final output ----
        if not draft.startswith(f"# {title}"):
            draft = f"# {title}\n\n" + draft

        # Annotate quality issues at the top so they're impossible to miss.
        # `warnings` was initialised earlier in this function so the
        # revision/fact-fix stub-detection path can append to it.
        if max_revisions > 0 and not approved:
            warnings.append("reviewer did not explicitly approve this draft")
        if fact_fix_failed:
            warnings.append("fact-check revision failed — paths/structs may be hallucinated")
        if warnings:
            warning_block = (
                "> ⚠ **UNVERIFIED DRAFT** — "
                + "; ".join(warnings)
                + ". Treat claims as suspect until manually reviewed.\n\n"
            )
            # Insert after the H1 title
            head, sep, tail = draft.partition("\n")
            draft = head + sep + "\n" + warning_block + tail

        # Provenance footer (LLM model + timestamp).
        draft = draft.rstrip() + _provenance_footer()

        # Back up the previous file (if any) just before we overwrite it.
        # Done late on purpose — see the note at the top of run_chapter().
        if os.path.exists(output_path):
            try:
                os.rename(output_path, backup_path)
                had_backup = True
            except OSError as e:
                print(f"  ⚠ could not back up existing {output_path}: {e}")

        # Atomic write — a crash here leaves either the backup or the new file.
        _atomic_write(output_path, draft)
        success = True

        lines = draft.count("\n")
        rev_label = f" after {revision} revision(s)" if revision > 0 else ""
        print(f"  ✓ wrote {lines} lines to {output_path}{rev_label}")
        banner = _format_stats_banner(stats)
        if banner:
            print(banner)
        return True

    finally:
        # Commit-or-rollback semantics for the previous output.
        if had_backup:
            if success:
                # Fresh draft is on disk; drop the backup.
                try:
                    os.remove(backup_path)
                except OSError:
                    pass
            elif not os.path.exists(output_path):
                # Nothing was written; restore the previous good file.
                try:
                    os.rename(backup_path, output_path)
                    print(f"  restored previous {output_path} from backup")
                except OSError as e:
                    print(f"  ✗ could not restore backup: {e}")


# --- Mermaid post-process sanitizer ---------------------------------------
#
# Writers occasionally emit a flowchart whose `subgraph NAME` line uses an
# id that already exists as a node in the same diagram. Mermaid then
# tries to make the node a child of a subgraph with the same id and
# refuses to render with:
#     "Setting <NAME> as parent of <NAME> would create a cycle"
# (Observed on `sys/netgraph/README.md` 2026-05-01 — `Userland` was both
# a node *and* a subgraph wrapping that node.)
#
# Like the JSONDecodeError fallback in `_extract_json`, this is a
# robustness floor: a deterministic post-process that fixes the diagram
# regardless of whether the writer follows prompt rules. The
# accompanying writer-prompt rule (in `build_chapter_prompt`'s
# flowchart hint) reduces the rate; this sanitizer guarantees the
# rendered output is valid even when the rule is ignored.

# Match a fenced mermaid code block. We capture the inner content and
# the leading/trailing fence so we can rewrite the block in place
# without disturbing surrounding markdown.
_MERMAID_BLOCK_RE = re.compile(
    r"(```mermaid\s*\n)(.*?)(\n```)", re.DOTALL,
)

# Detect a flowchart-family diagram. `graph TD` is the legacy alias.
_FLOWCHART_HEADER_RE = re.compile(
    r"^\s*(?:flowchart|graph)\b", re.MULTILINE,
)

# Subgraph header. Mermaid accepts:
#   subgraph id
#   subgraph id["Title"]
#   subgraph id [Title]
#   subgraph id Title with spaces
# The id is the first whitespace-separated token after `subgraph`,
# stripped of any trailing `[...]` shape.
_SUBGRAPH_HEADER_RE = re.compile(
    r"^(\s*)subgraph\s+([^\s\[\]\(\){}]+)(.*)$", re.MULTILINE,
)

# A standalone identifier used as a node or edge endpoint. We collect
# these by scanning every non-`subgraph` and non-`end` line for tokens
# that look like ids. An id is an alphanumeric/underscore run not
# starting with a digit. We deliberately do NOT try to fully parse
# mermaid syntax — we just need a superset of the node id set so that
# any subgraph id that ALSO appears as a node is detected.
_ID_TOKEN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


def _sanitize_mermaid_flowchart(block_body: str) -> str:
    """Rename subgraph ids that collide with node ids.

    Returns the rewritten block body. The colliding subgraph id gains a
    `_grp` suffix (or `_grp2`, `_grp3`, ... if `_grp` itself collides).
    Only the subgraph header line is changed — references inside the
    subgraph body that name the colliding id refer to the *node*, which
    is exactly what the writer meant in every observed case
    (subgraph used as a visual grouping wrapper, not as a referenced
    container).

    Non-flowchart diagrams pass through unchanged.
    """
    if not _FLOWCHART_HEADER_RE.search(block_body):
        return block_body

    # First pass: collect subgraph ids and "everything else" ids.
    subgraph_ids: List[Tuple[int, str]] = []  # (line_idx, id)
    other_ids: set = set()

    lines = block_body.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Skip the header line and `end` markers.
        if not stripped or stripped == "end":
            continue
        m = _SUBGRAPH_HEADER_RE.match(line)
        if m:
            subgraph_ids.append((i, m.group(2)))
            continue
        # Skip the `flowchart TD` / `graph LR` header line.
        if re.match(r"^\s*(?:flowchart|graph)\b", line):
            continue
        # Strip bracketed/parenthesised label contents so labels with
        # the same text as a colliding id (e.g. `Foo["Foo (extra)"]`)
        # don't pollute the node-id set with words from the title.
        scrub = re.sub(r"\[[^\]]*\]", " ", line)
        scrub = re.sub(r"\([^)]*\)", " ", scrub)
        scrub = re.sub(r"\{[^}]*\}", " ", scrub)
        for tok in _ID_TOKEN_RE.findall(scrub):
            other_ids.add(tok)

    # Mermaid keywords that aren't real node ids — we'd never collide
    # with them on purpose, but the token scan picks them up.
    _MERMAID_KEYWORDS = {
        "flowchart", "graph", "subgraph", "end", "TD", "TB", "BT",
        "LR", "RL", "direction", "click", "class", "classDef",
        "linkStyle", "style",
    }
    other_ids -= _MERMAID_KEYWORDS

    # Find collisions and pick a fresh name for each.
    used_ids = other_ids | {sid for _, sid in subgraph_ids}
    rename: Dict[str, str] = {}
    for _, sid in subgraph_ids:
        if sid not in other_ids or sid in rename:
            continue
        candidate = f"{sid}_grp"
        n = 2
        while candidate in used_ids:
            candidate = f"{sid}_grp{n}"
            n += 1
        rename[sid] = candidate
        used_ids.add(candidate)

    if not rename:
        return block_body

    # Rewrite each colliding subgraph header in place. Other lines
    # (including any references to the original id inside the subgraph
    # body) are intentionally left alone — those references resolve to
    # the node, which is what the writer meant.
    for i, sid in subgraph_ids:
        if sid not in rename:
            continue
        new_id = rename[sid]
        m = _SUBGRAPH_HEADER_RE.match(lines[i])
        if not m:
            continue
        leading, _id, rest = m.group(1), m.group(2), m.group(3)
        # Preserve any trailing ` ["Title"]` or ` Title text`. If the
        # original had no title, fall back to the original id as the
        # display title so the visual grouping label doesn't change.
        trailing = rest if rest.strip() else f' ["{sid}"]'
        lines[i] = f"{leading}subgraph {new_id}{trailing}"

    return "\n".join(lines)


def _sanitize_mermaid_blocks(text: str) -> str:
    """Apply `_sanitize_mermaid_flowchart` to every fenced mermaid block.

    No-op on documents without any mermaid blocks. Idempotent — running
    it twice on the same draft produces identical output.
    """
    def _sub(m: "re.Match") -> str:
        head, body, tail = m.group(1), m.group(2), m.group(3)
        return head + _sanitize_mermaid_flowchart(body) + tail
    return _MERMAID_BLOCK_RE.sub(_sub, text)


# Markdown link in a list-item line: `- [label](target)` (allows leading
# whitespace, optional bullet `*` or `-`). Used by the link sanitizer to
# decide whether a line is a *removable* list item versus an inline link
# in prose. We never drop inline-prose links (would mangle the sentence);
# we only rewrite them. List items with a broken link are dropped if no
# unique rewrite target exists.
_LIST_ITEM_LINK_RE = re.compile(
    r"^(\s*[-*]\s+)\[([^\]]+)\]\(([^)\s]+)\)(.*)$"
)
# Inline markdown link anywhere in the line.
_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _sanitize_chapter_links(
    content: str, current_file: str, all_chapter_files: "set[str]",
) -> Tuple[str, int, int]:
    """Fix or drop broken `*.md` links pointing at chapter READMEs.

    `current_file` is the chapter's path relative to SRC_ROOT (e.g.
    ``sys/kern/README_locking.md``); `all_chapter_files` is the set of
    all chapter README paths relative to SRC_ROOT.

    For every markdown link `[label](target.md[#anchor])`:
      1. Resolve `target` relative to `os.path.dirname(current_file)`.
      2. If it lands on a real chapter file → leave alone.
      3. Else, look up chapter files whose basename matches the target's
         basename. If exactly one such chapter exists → rewrite the link
         to point there (correct relative path from current_file's dir,
         preserving any `#anchor`). If zero or many exist:
            - List-item links: drop the entire list-item line.
            - Inline-prose links: leave alone (we won't mangle prose).

    Background: legacy navigation code emitted See Also links as if every
    chapter lived directly under `sys/`, so e.g. a link from
    `sys/kern/README_locking.md` to the buffer-cache chapter was written
    as ``vm/README_bcache.md`` (resolves to `sys/kern/vm/...`, which does
    not exist). The fixed `_add_see_also_links` only catches a subset of
    these stale links; this sanitizer is the deterministic floor that
    repairs (or removes) the rest. Idempotent: a clean document is left
    untouched.

    Returns ``(new_content, rewritten, dropped)``.
    """
    rewritten = 0
    dropped = 0

    chapter_files = set(all_chapter_files)
    chapter_files.discard(current_file)
    # basename -> set of chapter paths with that basename
    by_basename: Dict[str, set] = {}
    # last-two-path-components -> set of chapter paths (e.g. "vm/README.md")
    # The writer's broken link almost always carries the *correct*
    # parent dir hint (e.g. `vm/README.md`) but the wrong number of `..`
    # in front. Disambiguating by the last 2 components rescues
    # `README.md` collisions where pure basename matching would have to
    # drop the line.
    by_tail2: Dict[str, set] = {}
    for f in chapter_files:
        by_basename.setdefault(os.path.basename(f), set()).add(f)
        parts = f.split("/")
        if len(parts) >= 2:
            by_tail2.setdefault("/".join(parts[-2:]), set()).add(f)

    current_dir = os.path.dirname(current_file)

    def _is_chapter_target(target: str) -> bool:
        """True if `target` resolves to an existing chapter file."""
        tgt = target.split("#", 1)[0]
        if not tgt.endswith(".md"):
            return False
        joined = os.path.normpath(os.path.join(current_dir, tgt))
        return joined in chapter_files or os.path.isfile(
            os.path.join(SRC_ROOT, joined)
        )

    def _unique_rewrite(target: str) -> Optional[str]:
        """Return a corrected target if the link uniquely identifies a
        chapter; else None.

        Matches in two passes: first by the trailing two path components
        (e.g. `vm/README.md` — picks the one chapter with that suffix),
        then by basename alone. The two-component pass is what saves
        every `vm/README.md` style link in the corpus where bare basename
        matching collides across chapters.
        """
        tgt, _, anchor = target.partition("#")
        if not tgt.endswith(".md"):
            return None
        anchor_suffix = f"#{anchor}" if anchor else ""
        # Normalise: strip leading `./` and any leading `../`. The legacy
        # bug's whole signature is "wrong number of `..`"; collapsing them
        # lets us match the intended tail.
        tail = tgt
        while tail.startswith("./"):
            tail = tail[2:]
        while tail.startswith("../"):
            tail = tail[3:]
        # Try last-two-components match first.
        parts = tail.split("/")
        if len(parts) >= 2:
            key2 = "/".join(parts[-2:])
            cands2 = by_tail2.get(key2, set())
            if len(cands2) == 1:
                (real,) = cands2
                new_rel = os.path.relpath(
                    real, start=current_dir if current_dir else "."
                )
                return new_rel + anchor_suffix
        # Fall back to bare basename.
        cands1 = by_basename.get(os.path.basename(tgt), set())
        if len(cands1) == 1:
            (real,) = cands1
            new_rel = os.path.relpath(
                real, start=current_dir if current_dir else "."
            )
            return new_rel + anchor_suffix
        return None

    out_lines: List[str] = []
    for line in content.split("\n"):
        m = _LIST_ITEM_LINK_RE.match(line)
        if m:
            prefix, label, target, suffix = (
                m.group(1), m.group(2), m.group(3), m.group(4),
            )
            # Skip non-relative or non-.md targets.
            tgt_for_check = target.split("#", 1)[0]
            if (target.startswith(("http://", "https://", "#", "mailto:"))
                    or not tgt_for_check.endswith(".md")):
                out_lines.append(line)
                continue
            if _is_chapter_target(target):
                out_lines.append(line)
                continue
            # Broken — try unique rewrite, else drop the list item.
            fix = _unique_rewrite(target)
            if fix is not None:
                out_lines.append(f"{prefix}[{label}]({fix}){suffix}")
                rewritten += 1
            else:
                dropped += 1
            continue

        # Inline-prose links: rewrite if unique, else leave alone.
        def _sub_inline(mm: "re.Match") -> str:
            nonlocal rewritten
            label, target = mm.group(1), mm.group(2)
            if (target.startswith(("http://", "https://", "#", "mailto:"))
                    or not target.split("#", 1)[0].endswith(".md")):
                return mm.group(0)
            if _is_chapter_target(target):
                return mm.group(0)
            fix = _unique_rewrite(target)
            if fix is None:
                return mm.group(0)
            rewritten += 1
            return f"[{label}]({fix})"

        out_lines.append(_INLINE_LINK_RE.sub(_sub_inline, line))

    new_content = "\n".join(out_lines)
    # Dedupe exact-duplicate list-item links inside the See Also section.
    # Rewriting two stylistically different broken links (e.g.
    # ``kern/README_process.md`` and ``README_process.md`` from
    # `sys/kern/README_jail.md`) normalises both to the same target,
    # so the section ends up with a doubled entry. Drop the second
    # occurrence; the See Also section should never repeat a link.
    new_content, deduped = _dedupe_see_also_section(new_content)
    dropped += deduped
    if dropped:
        # Collapse the blank-line runs that dropping list items can leave
        # behind, but keep paragraph separation.
        new_content = re.sub(r"\n{3,}", "\n\n", new_content)
    return new_content, rewritten, dropped


def _dedupe_see_also_section(content: str) -> Tuple[str, int]:
    """Drop exact-duplicate ``- [label](target)`` lines inside the See
    Also section. Returns ``(new_content, dropped_count)``.

    Scope is intentionally narrow (See Also only) so unrelated lists in
    the body are left untouched. Match key is ``(label, target)`` — only
    *exact* dupes count as redundant; two links with different labels to
    the same target stay.
    """
    sa = content.find("\n## See Also")
    if sa == -1:
        return content, 0
    body_start = content.find("\n", sa + len("\n## See Also"))
    if body_start == -1:
        return content, 0
    body_start += 1
    next_h2 = re.search(r"(?m)^## ", content[body_start:])
    body_end = body_start + next_h2.start() if next_h2 else len(content)

    seen: set = set()
    dropped = 0
    out: List[str] = []
    for line in content[body_start:body_end].split("\n"):
        m = _LIST_ITEM_LINK_RE.match(line)
        if m:
            key = (m.group(2), m.group(3))
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
        out.append(line)
    if dropped == 0:
        return content, 0
    return content[:body_start] + "\n".join(out) + content[body_end:], dropped


# docs.freebsd.org URLs that point at a handbook/articles slug. Captures
# the section ("books/handbook" or "articles") and the slug (first path
# segment after that). Trailing path / anchor / query are tolerated but
# not used for validation — the slug alone determines whether the page
# exists. Both http(s) and the writer's occasional bare-relative
# `books/handbook/<slug>/` are validated by the slug check; the bare-
# relative form is always broken (no host) and gets caught by the same
# code path.
_DOC_FREEBSD_URL_RE = re.compile(
    r"^https?://docs\.freebsd\.org/(?:[a-z]{2}/)?"
    r"(books/handbook|articles)/([A-Za-z0-9_+-]+)"
)
# Bare `books/handbook/<slug>/` or `articles/<slug>/` written as if it
# were a relative link (no scheme, no host). Always broken — markdown
# resolves it relative to the chapter file. Caught here so the dropper
# treats it the same way as a bogus full URL.
_DOC_FREEBSD_RELATIVE_RE = re.compile(
    r"^(?:\.{0,2}/)?(books/(?:handbook|arch-handbook)|articles)/"
    r"([A-Za-z0-9_+-]+)"
)


# Memoized known-slug index from $FREEBSD_DOC.
_KNOWN_DOC_SLUGS_CACHE: Optional[Dict[str, set]] = None


def _known_doc_slugs() -> Dict[str, set]:
    """Return ``{"books/handbook": {slug, ...}, "articles": {slug, ...}}``
    populated from ``$FREEBSD_DOC``. Empty sets if the tree isn't present.

    A "slug" is a directory name directly under ``books/handbook/`` or
    ``articles/`` in the freebsd-doc source — these are the URL path
    segments docs.freebsd.org publishes (``/en/books/handbook/<slug>/``).
    Memoized for the lifetime of the process; the doc tree doesn't change
    mid-run.
    """
    global _KNOWN_DOC_SLUGS_CACHE
    if _KNOWN_DOC_SLUGS_CACHE is not None:
        return _KNOWN_DOC_SLUGS_CACHE
    out: Dict[str, set] = {"books/handbook": set(), "articles": set()}
    for section in ("books/handbook", "articles"):
        root = os.path.join(FREEBSD_DOC, section)
        if not os.path.isdir(root):
            continue
        try:
            for name in os.listdir(root):
                if name.startswith(("_", ".")):
                    continue
                if os.path.isdir(os.path.join(root, name)):
                    out[section].add(name)
        except OSError:
            pass
    _KNOWN_DOC_SLUGS_CACHE = out
    return out


def _doc_url_is_known(target: str) -> Optional[bool]:
    """Return True if ``target`` is a docs.freebsd.org-style link whose
    slug exists in $FREEBSD_DOC; False if it's a docs.freebsd.org-style
    link with an unknown slug; None if the target doesn't look like a
    handbook/articles URL at all (caller should leave it alone).

    "docs.freebsd.org-style" covers two shapes:
      * Full URL: ``https://docs.freebsd.org/en/books/handbook/<slug>/...``
      * Bare relative: ``books/handbook/<slug>/...`` or
        ``articles/<slug>/...`` (writer drift — markdown won't resolve
        these to anything real).

    If the doc tree isn't available (no $FREEBSD_DOC on disk), every
    handbook URL is treated as unknown — this is the right failure mode:
    the sanitizer falls back to "drop on doubt", which is what the user
    asked for. Sites without freebsd-doc shouldn't ship handbook links
    at all.
    """
    m = _DOC_FREEBSD_URL_RE.match(target) or _DOC_FREEBSD_RELATIVE_RE.match(
        target
    )
    if m is None:
        return None
    section, slug = m.group(1), m.group(2)
    # arch-handbook is a separate book; we don't ship that book's slugs
    # in our index. Any `books/arch-handbook/...` reference from the
    # writer is hallucinated until proven otherwise — the legitimate
    # handbook lives at `books/handbook/`.
    if section == "books/arch-handbook":
        return False
    known = _known_doc_slugs().get(section, set())
    return slug in known


def _sanitize_doc_urls(content: str, current_file: str) -> Tuple[str, int]:
    """Drop or strip docs.freebsd.org links whose slug isn't real.

    Mirrors `_sanitize_chapter_links`'s shape but for FreeBSD-handbook
    and -articles URLs. The writer occasionally hallucinates a plausible
    slug ("geom-class" instead of "geom") — every existing fact-check
    layer passes because the URL never points into the source tree.
    Validating against the on-disk freebsd-doc index catches it
    deterministically.

    Behaviour per shape:
      * List-item link with a bogus slug → drop the whole list item
        (same as `_sanitize_chapter_links` does for broken `.md` links).
      * Inline-prose link with a bogus slug → strip the link wrapper,
        keep the label text. We won't mangle a sentence by deleting it
        whole, but we will neuter a misleading hyperlink.
      * Non-docs.freebsd.org URLs → untouched.
      * Real slugs → untouched.

    Returns ``(content, dropped)`` for diagnostics. ``current_file`` is
    accepted for symmetry with the other sanitizers; not currently used.
    """
    del current_file  # symmetry; not needed for URL validation
    dropped = 0

    out_lines: List[str] = []
    for line in content.split("\n"):
        m = _LIST_ITEM_LINK_RE.match(line)
        if m:
            target = m.group(3)
            verdict = _doc_url_is_known(target)
            if verdict is False:
                dropped += 1
                continue
            out_lines.append(line)
            continue

        # Inline-prose links: strip wrapper if bogus, leave label.
        def _sub_inline(mm: "re.Match") -> str:
            nonlocal dropped
            label, target = mm.group(1), mm.group(2)
            verdict = _doc_url_is_known(target)
            if verdict is False:
                dropped += 1
                return label
            return mm.group(0)

        out_lines.append(_INLINE_LINK_RE.sub(_sub_inline, line))

    new_content = "\n".join(out_lines)
    if dropped:
        new_content = re.sub(r"\n{3,}", "\n\n", new_content)
    return new_content, dropped


# Backtick-wrapped path inside a list-item line in See Also. Captures the
# path (and only the path) so we can verify it exists on disk before
# turning it into a markdown link. We deliberately match only inside list
# items (leading bullet) — bare backtick paths in prose elsewhere are
# inline code references, not navigation, and rewriting them would change
# meaning. The lookahead/lookbehind on `[` and `]` skips paths that are
# already inside a markdown link (idempotency).
_BACKTICK_PATH_RE = re.compile(
    r"(?<!\[)`("
    # Path with extension: foo/bar.c, baz.h, etc.
    r"[A-Za-z0-9_./+-]+\.[A-Za-z0-9]+"
    # Path ending in a slash (directory marker): sys/kern/
    r"|[A-Za-z0-9_./+-]+/"
    # Slash-bearing extensionless path: tests/README, gnu/COPYING.
    # Final _exists() check still gates this — false positives in
    # prose only become real links if they happen to name a file
    # under SRC_ROOT, which is acceptably rare in See Also context.
    r"|[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+"
    r")`(?!\])"
)


def _link_see_also_source_paths(
    content: str, current_file: str,
) -> Tuple[str, int]:
    """Wrap bare backtick source-file paths in See Also as relative
    markdown links. Returns ``(new_content, linked_count)``.

    Browser-readable rendering: the writer emits canonical paths from the
    repo root (e.g. ``sys/kern/kern_mutex.c``) inside backticks, but
    backticks are not clickable. A reader looking at the rendered README
    on GitHub / cgit / a local file:// browser cannot jump to the source
    file the way they can jump to a sibling chapter README.

    We rewrite each bare backtick path that
      1. lives inside the ``## See Also`` section,
      2. is on a list-item line,
      3. is not already inside a markdown link, and
      4. resolves to an existing file or directory under SRC_ROOT
    into ``[`canonical/path`](relpath)`` where ``relpath`` is computed
    from the chapter's directory.

    Out-of-scope on purpose:
      - Backtick paths outside See Also (Architecture/Deep Dive prose
        uses backticks as inline code, not as navigation).
      - Paths that don't exist on disk (we don't fabricate links).
      - Paths inside fenced code blocks (the regex won't match list-item
        bullets there — code blocks don't start lines with ``- ``).
    Idempotent — re-running on already-linked content is a no-op because
    the regex skips paths already wrapped in ``[ ... ]``.
    """
    # Accept either a leading `## See Also` (file starts with the
    # heading — rare, but happens in tests / minimal inputs) or the
    # normal `\n## See Also` mid-document case.
    if content.startswith("## See Also"):
        body_start_tag = 0
    else:
        sa = content.find("\n## See Also")
        if sa == -1:
            return content, 0
        body_start_tag = sa + 1  # position of '#' itself
    body_start = content.find("\n", body_start_tag + len("## See Also"))
    if body_start == -1:
        return content, 0
    body_start += 1
    next_h2 = re.search(r"(?m)^## ", content[body_start:])
    body_end = body_start + next_h2.start() if next_h2 else len(content)

    current_dir = os.path.dirname(current_file)
    linked = 0

    def _exists(path: str) -> bool:
        # Both files and directories are valid link targets; reject
        # absolute paths or anything escaping SRC_ROOT.
        if path.startswith("/") or ".." in path.split("/"):
            return False
        full = os.path.join(SRC_ROOT, path)
        return os.path.exists(full)

    out_lines: List[str] = []
    for line in content[body_start:body_end].split("\n"):
        # Only rewrite list-item lines. Bare-backtick paths in headings
        # or prose paragraphs in See Also (rare) stay untouched.
        if not re.match(r"^\s*[-*]\s", line):
            out_lines.append(line)
            continue

        def _sub(m: "re.Match") -> str:
            nonlocal linked
            path = m.group(1)
            if not _exists(path):
                return m.group(0)
            relpath = os.path.relpath(
                path, start=current_dir if current_dir else "."
            )
            linked += 1
            return f"[`{path}`]({relpath})"

        out_lines.append(_BACKTICK_PATH_RE.sub(_sub, line))

    if linked == 0:
        return content, 0
    return content[:body_start] + "\n".join(out_lines) + content[body_end:], linked


# Inline man-page reference: `name(N)` where N is 1..9 (optional
# trailing letter for subsections like 3lua, 9a). The name must start
# with a letter or underscore; we allow dots/hyphens/underscores in the
# middle so refs like `cap_enter(2)`, `pf.conf(5)`, `link-elf(8)`
# match. Anchored with a non-word lookbehind so we don't match the
# tail of a longer identifier (e.g. `frob_O(1)` shouldn't match).
_MANREF_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"([A-Za-z_][A-Za-z0-9_+.-]*)\(([1-9][a-z]*)\)"
)

# Matches any fenced code block (``` … ```), used to mask out regions
# where we should NOT rewrite man references (the content is sample
# code, not prose).
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def _build_manpage_index(src_root: str) -> Dict[str, str]:
    """Walk `src_root` once and return ``{ "name(N)": relpath }`` for
    every plausible man-page file.

    "Plausible" means filename matches `<stem>.<section>` where stem
    starts with a letter/underscore and section is `[1-9][a-z]*`. This
    rejects the noise files (`RELEASE-4.4`, version strings ending in
    a digit) that share the suffix shape.

    On collisions (same `name(N)` in multiple paths — common in the
    OpenSSL contrib tree), the tiebreaker is:
      1. Prefer paths under `share/man/manN/` (the canonical source).
      2. Otherwise, take the first one walked.

    Builds the index lazily and caches in `_MANPAGE_INDEX_CACHE` keyed
    by `src_root`. Only invalidated when the process restarts; safe
    because `freebsd-src` doesn't change underneath us inside one
    Phase 4 invocation.
    """
    cached = _MANPAGE_INDEX_CACHE.get(src_root)
    if cached is not None:
        return cached

    name_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_+.-]*)\.([1-9][a-z]*)$")
    index: Dict[str, str] = {}

    for dirpath, _dirs, files in os.walk(src_root):
        for fn in files:
            m = name_re.match(fn)
            if not m:
                continue
            stem, section = m.group(1), m.group(2)
            key = f"{stem}({section})"
            full = os.path.join(dirpath, fn)
            relpath = os.path.relpath(full, src_root)
            existing = index.get(key)
            if existing is None:
                index[key] = relpath
                continue
            # Collision — prefer canonical share/man/man<N>/ location.
            canon_prefix = f"share/man/man{section[0]}/"
            if relpath.startswith(canon_prefix) and not existing.startswith(canon_prefix):
                index[key] = relpath
            # Otherwise keep the existing one.

    _MANPAGE_INDEX_CACHE[src_root] = index
    return index


# Per-process cache for `_build_manpage_index`. Keyed by SRC_ROOT
# string so a different `--src-root` invocation rebuilds correctly.
_MANPAGE_INDEX_CACHE: Dict[str, Dict[str, str]] = {}


def _link_manpage_refs(
    content: str, current_file: str,
) -> Tuple[str, int]:
    """Wrap inline ``name(N)`` man-page references as relative markdown
    links to the corresponding source-tree man-page file. Returns
    ``(new_content, linked_count)``.

    Browser-readable rendering: chapter prose mentions man pages
    constantly (``src.conf(5)``, ``ngctl(8)``, ``mbuf(9)``) but the
    rendered markdown shows them as plain text. This rewriter turns
    them into clickable links to the unformatted mdoc source under
    ``freebsd-src``. The reader can `make` the man page locally or
    cross-reference to ``man.freebsd.org`` themselves; we link to the
    source on disk so the link works offline and is self-contained.

    Behavior:
      1. Skip fenced code blocks (mask them out before scanning).
      2. Skip refs that already sit inside a markdown link
         ``[label](target)`` — the post-link content can include the
         text ``foo(N)`` but we don't double-wrap.
      3. Skip refs whose name doesn't resolve in the man-page index.
         False positives like ``O(1)`` (algorithmic complexity) get
         filtered for free because no `O.1` file exists.

    Idempotent: re-running on already-linked content is a no-op
    because the inside-link skip handles it.

    Out-of-scope on purpose: rewriting man refs that the writer
    accidentally placed inside backticks (e.g. `` `src.conf(5)` ``)
    — left untouched because they're styled as inline code. If the
    user wants those linked too, drop the backticks at the writer
    level rather than here.
    """
    index = _build_manpage_index(SRC_ROOT)
    if not index:
        return content, 0

    current_dir = os.path.dirname(current_file)

    # Mask fenced code blocks: replace each block with placeholders
    # that the regex won't touch, then restore after rewriting.
    fences: List[str] = []

    def _stash_fence(m: "re.Match") -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    masked = _FENCED_BLOCK_RE.sub(_stash_fence, content)

    # Build a set of (start, end) offsets inside markdown links
    # `[label](target)` — both the label region AND the target
    # region — so the man-ref regex can skip matches inside them.
    # Without this, ``[src.conf](https://...src.conf(5)...)`` would
    # get nested-rewritten.
    link_spans: List[Tuple[int, int]] = []
    for m in re.finditer(r"\[[^\]]*\]\([^)]*\)", masked):
        link_spans.append((m.start(), m.end()))

    def _in_link(pos: int) -> bool:
        # Linear scan is fine — typical chapter has < 50 links and
        # < 50 man refs.
        for s, e in link_spans:
            if s <= pos < e:
                return True
        return False

    linked = 0
    out_parts: List[str] = []
    last = 0
    for m in _MANREF_RE.finditer(masked):
        if _in_link(m.start()):
            continue
        key = f"{m.group(1)}({m.group(2)})"
        target_rel_to_src = index.get(key)
        if target_rel_to_src is None:
            continue
        target = os.path.relpath(
            target_rel_to_src,
            start=current_dir if current_dir else ".",
        )
        # Preserve inline-code styling: if the ref is wrapped in single
        # backticks (`src.conf(5)`), produce `[`src.conf(5)`](path)`
        # so the styling survives. Without this, naive replacement
        # would output ``[src.conf(5)](path)`` — backticks outside the
        # link, which renders as literal text in most md engines.
        ref_start = m.start()
        ref_end = m.end()
        wrapped = (
            ref_start > 0
            and ref_end < len(masked)
            and masked[ref_start - 1] == "`"
            and masked[ref_end] == "`"
        )
        if wrapped:
            out_parts.append(masked[last:ref_start - 1])
            out_parts.append(f"[`{key}`]({target})")
            last = ref_end + 1
        else:
            out_parts.append(masked[last:ref_start])
            out_parts.append(f"[{key}]({target})")
            last = ref_end
        linked += 1
    out_parts.append(masked[last:])
    new_masked = "".join(out_parts)

    # Restore fences.
    def _restore(m: "re.Match") -> str:
        return fences[int(m.group(1))]
    new_content = re.sub(r"\x00FENCE(\d+)\x00", _restore, new_masked)

    return new_content, linked


# Matches a glossary definition line. Two shapes are accepted because the
# corpus contains both: the catalog template asks for `**term** — text`,
# while the one chapter that shipped a Glossary before it became a default
# (sys/vm/README.md) used a bullet, `- **term** — text`. Accepting only the
# templated shape would silently skip the sole real-world example.
#
# The separator is an em/en dash or a colon. A plain hyphen is deliberately
# NOT accepted: `- **Slab** - text` is indistinguishable from a bullet list
# whose item happens to start bold, and matching it pulls in ordinary prose.
_GLOSSARY_ENTRY_RE = re.compile(
    r"^\s*(?:[-*]\s+)?\*\*(?P<term>[^*\n]{2,60}?)\*\*\s*(?:[—–:])",
    re.MULTILINE,
)

# A term is linkable only if it looks like a term, not a sentence. Glossary
# lines occasionally carry a parenthetical or a trailing qualifier; those
# make bad link anchors and bad regex targets.
_GLOSSARY_TERM_OK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _/()+-]{1,40}$")

# Own fence matcher, deliberately not shared. `_FENCED_BLOCK_RE` is defined
# twice in this file (once near the man-page linker, once near the struct
# verifier) and the later definition wins for every caller — the second one
# requires a newline after the opening fence, so single-line ``` blocks are
# not masked. That is a pre-existing bug in the man-ref linker, not one to
# inherit here. This pattern masks both shapes.
_GLOSS_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _parse_glossary_entries(content: str) -> Dict[str, str]:
    """Return ``{term: anchor_slug}`` for each entry in a Glossary section.

    Insertion-ordered (plain dict) so the caller links deterministically.
    Returns an empty mapping when the chapter has no Glossary — which, on
    the corpus as of 2026-08-29, is every chapter but one. That is the
    reason step 2 pairs the linker with promoting Glossary to a default
    section: a linker alone would have had almost nothing to point at.
    """
    section = _find_glossary_section(content)
    if not section.strip():
        return {}
    out: Dict[str, str] = {}
    for m in _GLOSSARY_ENTRY_RE.finditer(section):
        term = m.group("term").strip()
        # Strip inline code styling the writer may have added: `**`vnode`**`.
        term = term.strip("`").strip()
        if not _GLOSSARY_TERM_OK_RE.match(term):
            continue
        out.setdefault(term, _GLOSSARY_ANCHOR)
    return out


def _find_glossary_span(text: str) -> Optional[Tuple[int, int]]:
    """Offsets of the whole Glossary section, heading included, or None.

    `_find_glossary_section` returns the body text; the linker needs the
    span so it can refuse to rewrite anything inside it. Same pattern, so
    the two agree on what counts as a Glossary.
    """
    m = re.search(
        r"^##+\s*Glossary\s*$(.*?)(?=^##\s|\Z)",
        text, re.MULTILINE | re.DOTALL,
    )
    return (m.start(), m.end()) if m else None


# Every glossary term links to the Glossary SECTION, not to a per-term
# anchor. GitHub mints anchors for *headings* only — a term rendered as a
# bolded list item (`- **Slab** — ...`) gets no anchor at all, so a
# `#slab` target silently resolves to nothing and the reader lands at the
# top of the file. Verified against the real sys/vm/README.md on
# 2026-08-29: it has `## Glossary` and eight bolded terms beneath it, and
# `#zone` / `#slab` match no heading in the document.
#
# The alternative — emitting `<a id="slab"></a>` into each definition —
# would give exact targets, but it means the linker mutates the Glossary
# body it is otherwise forbidden to touch, and raw HTML degrades in the
# plain-text reading that these in-tree READMEs are also meant for. A
# section link always resolves and puts the definition on screen.
_GLOSSARY_ANCHOR = "glossary"


def _github_anchor(text: str) -> str:
    """Slugify text the way GitHub mints a heading anchor.

    Lowercase, spaces to hyphens, drop everything that isn't
    alphanumeric/hyphen/underscore. Retained for headings; glossary terms
    use `_GLOSSARY_ANCHOR` because bolded list items get no anchor (see
    the note above).
    """
    slug = text.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug.strip("-")


def _link_glossary_first_use(
    content: str, current_file: str,
    external_terms: Optional[dict] = None,
) -> Tuple[str, int]:
    """Link the FIRST prose use of each glossary term to its definition.

    Fourth sibling to `_link_see_also_source_paths` and
    `_link_manpage_refs`, and it follows their rules exactly: mask fenced
    blocks, never touch text already inside a markdown link, never touch
    inline code, and be idempotent.

    Two link targets:
      * in-chapter — a term defined in this chapter's own Glossary links
        to ``#anchor``;
      * cross-chapter — a term defined in ANOTHER chapter's Glossary links
        to ``<relpath>#anchor``, with the relative path computed from
        `current_file`, never assumed. (The ch17 incident: a chapter whose
        `output_file` gained a directory level shipped 15 links that were
        all one ``../`` too shallow. Depth is derived, never hard-coded.)

    `external_terms` maps ``term -> (output_file, anchor)``. When a term is
    defined both here and elsewhere, the local definition wins: a reader
    should not be sent to another file for something defined on the page.

    Only the first prose occurrence is linked. Linking every mention turns
    a chapter into a sea of blue and re-reads as noise; the first use is
    where a reader actually needs the escape hatch.
    """
    entries = _parse_glossary_entries(content)
    external_terms = external_terms or {}
    if not entries and not external_terms:
        return content, 0

    # Local definitions shadow external ones.
    targets: Dict[str, str] = {}
    for term, anchor in entries.items():
        targets[term] = f"#{anchor}"
    current_dir = os.path.dirname(current_file)
    for term, (other_file, anchor) in external_terms.items():
        if term in targets or other_file == current_file:
            continue
        rel = os.path.relpath(
            other_file, start=current_dir if current_dir else ".")
        targets[term] = f"{rel}#{anchor}"
    if not targets:
        return content, 0

    # Mask fenced blocks exactly as _link_manpage_refs does.
    fences: List[str] = []

    def _stash_fence(m: "re.Match") -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    masked = _GLOSS_FENCE_RE.sub(_stash_fence, content)

    # Never link inside the Glossary section itself — a definition that
    # links to its own anchor is circular and looks broken.
    gloss_span = _find_glossary_span(masked)

    # Never link inside an existing markdown link, and never inside inline
    # code. Inline-code spans matter more here than for man refs: kernel
    # prose writes `vnode` in backticks constantly, and those are already
    # styled as code — wrapping them would double up the markup.
    skip_spans: List[Tuple[int, int]] = []
    for m in re.finditer(r"\[[^\]]*\]\([^)]*\)", masked):
        skip_spans.append((m.start(), m.end()))
    for m in re.finditer(r"`[^`\n]+`", masked):
        skip_spans.append((m.start(), m.end()))
    if gloss_span:
        skip_spans.append(gloss_span)
    # Headings: linking a term inside an H2 breaks the heading's own anchor.
    for m in re.finditer(r"^#{1,6} .*$", masked, re.MULTILINE):
        skip_spans.append((m.start(), m.end()))

    # See Also is a link list, owned by _add_see_also_links and the two
    # path linkers. A fifth link threaded into a bullet that is already a
    # link reads as noise. Observed on README_transport.md in the
    # 2026-08-29 dry run.
    for m in re.finditer(
        r"^##+\s*See Also\s*$.*?(?=^##\s|\Z)", masked,
        re.MULTILINE | re.DOTALL,
    ):
        skip_spans.append((m.start(), m.end()))

    # Double-quoted spans. In this corpus a quoted phrase is either a
    # verbatim quote from a source comment or a colloquialism, and
    # neither should pick up a definition link. This is what caught
    # `a "drop zone"` in README_internals.md, where "zone" is ordinary
    # English and not the UMA term — the one homograph false positive
    # in the whole corpus.
    for m in re.finditer(r"\"[^\"\n]{1,200}\"", masked):
        skip_spans.append((m.start(), m.end()))

    def _skip(pos: int, end: int) -> bool:
        for s, e in skip_spans:
            if s <= pos < e or s < end <= e:
                return True
        return False

    # Longest term first: linking "page fault" must win over "page" when
    # both are defined, otherwise the shorter match eats the longer one.
    ordered = sorted(targets, key=len, reverse=True)

    linked = 0
    # Track regions we have already rewritten so a shorter term cannot
    # match inside a link we just inserted.
    consumed: List[Tuple[int, int]] = []

    def _consumed(pos: int, end: int) -> bool:
        for s, e in consumed:
            if s <= pos < e or s < end <= e:
                return True
        return False

    for term in ordered:
        # Case-insensitive for multi-word/lowercase terms, case-sensitive
        # for short all-caps ones (KVM, UMA, DMA) so "uma" inside an
        # identifier fragment does not match. Same rule as _term_pattern.
        flags = 0 if (term.isupper() and len(term) <= 5) else re.IGNORECASE
        pat = re.compile(rf"\b{re.escape(term)}\b", flags)

        # Idempotency guard. "Link the first prose use" is not the same
        # rule as "link the first UNLINKED use": on a second pass the
        # already-linked first use is skipped as inside-a-link, and
        # without this the loop would happily link the *next* mention,
        # accumulating one more link per run.
        #
        # Keyed on the term text, not on the target: every term in a
        # chapter now shares one target (`#glossary`), so a target-only
        # check would see the first term's link and skip all the others.
        if re.search(
            rf"\[{pat.pattern}\]\({re.escape(targets[term])}\)",
            masked, flags,
        ):
            continue

        for m in pat.finditer(masked):
            if _skip(m.start(), m.end()) or _consumed(m.start(), m.end()):
                continue
            # Rewrite this one occurrence and stop scanning this term.
            replacement = f"[{m.group(0)}]({targets[term]})"
            masked = masked[:m.start()] + replacement + masked[m.end():]
            delta = len(replacement) - (m.end() - m.start())
            # Shift previously recorded spans that sit after the edit.
            skip_spans = [
                (s + delta if s >= m.end() else s,
                 e + delta if e >= m.end() else e)
                for s, e in skip_spans
            ]
            consumed = [
                (s + delta if s >= m.end() else s,
                 e + delta if e >= m.end() else e)
                for s, e in consumed
            ]
            consumed.append((m.start(), m.start() + len(replacement)))
            skip_spans.append((m.start(), m.start() + len(replacement)))
            linked += 1
            break

    def _restore(m: "re.Match") -> str:
        return fences[int(m.group(1))]
    new_content = re.sub(r"\x00FENCE(\d+)\x00", _restore, masked)
    return new_content, linked


def _extract_json(text: str) -> Optional[dict]:
    """Extract a JSON object from LLM output (may have prose around it).

    Returns None on any parse failure — never raises. Two failure modes
    we have actually observed:
      1. The text contains no JSON-looking block at all.
      2. The text contains a brace-balanced block, but the block has
         unescaped inner double quotes inside string values
         (e.g. `"completeness": "PASS: ... "background processing" ..."`)
         which break `json.loads`.
    The caller (run_chapter) already has a parse-retry-once path for
    None, so returning None here turns a hard crash into a single
    benign retry. Raising would kill the whole chapter mid-loop and
    discard any in-progress draft — see FUTURE_IMPROVEMENTS.md
    "Reviewer emits JSON with unescaped inner quotes" for the
    post-mortem.
    """
    # Try the full text first
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find a JSON block
    # Match { ... } with nested braces
    depth = 0
    start = None
    for i, c in enumerate(text):
        if c == '{':
            if start is None:
                start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i+1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


# ---------------------------------------------------------------------------
# 4b. Structured Fact-Checking
# ---------------------------------------------------------------------------


## Top-level directories of the FreeBSD source tree that are worth
## fact-checking when the writer cites a path. Anything outside this
## set (build artefacts, OS-specific dirs, etc.) is skipped because the
## verification step would produce noise rather than catch real
## hallucinations. Keep this in sync with the actual src layout.
_FREEBSD_TOP_DIRS = (
    'sys/', 'share/', 'stand/', 'lib/', 'libexec/',
    'bin/', 'sbin/', 'usr.bin/', 'usr.sbin/',
    'contrib/', 'tools/', 'gnu/', 'cddl/', 'crypto/',
    'kerberos5/', 'rescue/', 'secure/', 'tests/', 'targets/',
    'release/', 'sbin/', 'etc/',
)


# Extensionless filenames common in a FreeBSD checkout. Without these the
# directory branch below would skip `Makefile` and `COPYING`, which is how
# `gnu/Makefile` slipped past the extension-based extractors.
_EXTENSIONLESS_FILES = frozenset({
    'Makefile', 'Makefile.inc', 'Makefile.inc1', 'Makefile.libcompat',
    'COPYING', 'COPYING.LIB', 'README', 'Kyuafile', 'LICENSE',
    'MAINTAINERS', 'UPDATING', 'ObsoleteFiles.inc',
})


def _extract_file_paths(text: str) -> List[str]:
    """Extract claimed FreeBSD source file paths from markdown text."""
    # Match paths like sys/vm/vm_page.c, share/man/man9/foo.9, etc.
    # Look for backtick-quoted or bare paths
    # Trailing `(?!\w)` prevents the extension alternation from biting
    # into a longer extension — without it, `.s` would match the `s` in
    # `gdb-add-index.sh` and yield a bogus `.s` path.
    paths = []
    for m in re.finditer(
        r'(?:`)([^`\s]+(?:/[^`\s]+)*\.(?:c|h|s|rs|md|9|4|5|7|8))(?!\w)', text
    ):
        p = m.group(1)
        if p.startswith(_FREEBSD_TOP_DIRS):
            paths.append(p)
    # Also match bare paths (not in backticks but still file-like).
    # The character class is intentionally restrictive so we don't pick
    # up surrounding punctuation.
    top_alt = "|".join(re.escape(d.rstrip('/')) for d in _FREEBSD_TOP_DIRS)
    bare_re = (
        r'(?:^|\s)((?:' + top_alt + r')/\S+\.'
        r'(?:c|h|s|rs|9|4|5|7|8|adoc|mk|sh|py))(?!\w)'
    )
    for m in re.finditer(bare_re, text):
        p = m.group(1).strip()
        if p not in paths:
            paths.append(p)

    # Directory paths and extensionless files. Both branches above require a
    # file extension, so `gnu/`, `librescue`, `src/tests/bin`, and
    # `gnu/Makefile` were never extracted and therefore never verified —
    # ch1 (Source Tree, almost entirely directory prose) shipped 9 bogus
    # paths on 2026-04-30 while fact-check reported clean. `gnu/` in
    # particular was retired upstream (134a4c78d070 "Retire the GNU
    # subtree"), the same stale-training-data failure mode as fabricated
    # struct fields.
    #
    # Deliberately NOT gated on _FREEBSD_TOP_DIRS: that tuple still lists
    # `gnu/`, so filtering through it would whitelist exactly the retired
    # directory we need to catch. Instead take any backticked slash-path
    # and let _verify_file_paths decide, with the exemptions below removing
    # the shapes that are legitimately absent from a source checkout.
    for m in re.finditer(r'`([A-Za-z0-9_][A-Za-z0-9_./+-]*)`', text):
        raw = m.group(1)
        # A trailing slash is the writer explicitly marking a directory
        # (`librescue/`, `gnu/`). Note this BEFORE stripping it, otherwise
        # single-segment directory claims become indistinguishable from
        # bare words and get skipped by the separator test below.
        marked_dir = raw.endswith('/')
        p = raw.rstrip('/')
        if not p or p in paths:
            continue
        # Absolute paths name installed-system locations (/boot/kernel,
        # /usr/bin, /etc/rc.d). Correct prose in a layout chapter, and
        # legitimately not present in the source tree.
        if p.startswith('/'):
            continue
        # Needs to look like a path (contains a separator) or be a
        # well-known extensionless file. A bare word is far more likely to
        # be a command, variable, or struct field than a directory.
        if '/' not in p and not marked_dir and p not in _EXTENSIONLESS_FILES:
            continue
        # Skip things already covered by the extension-based branches.
        if os.path.splitext(p)[1] and p in paths:
            continue
        paths.append(p)

    return list(set(paths))


# Non-FreeBSD or non-C-symbol tokens that look like function/struct names
# when extracted from backticks but aren't. Keeping the fact-checker honest
# matters because every false positive in this list eats one or more steps
# of the writer's fact-fix budget — and bad fact-fixes have introduced
# regressions in the past (e.g. "fixing" `GENERIC` by removing a sentence
# that wasn't wrong).
_FACT_CHECK_IGNORE = frozenset({
    # Make targets / build-system phases
    "buildworld", "installworld", "buildkernel", "installkernel",
    "kernel-toolchain", "make", "world", "universe", "tinderbox",
    "doxygen", "checkworld", "delete-old", "check-old", "xdev",
    "native-xtools", "kernels", "worlds", "toolchains",
    # Make / kernel-config knobs (mostly all-caps with underscores)
    "GENERIC", "MINIMAL", "LINT", "NOTES",
    "TARGET", "TARGET_ARCH", "MACHINE", "MACHINE_ARCH",
    "OBJTOP", "OBJROOT", "DESTDIR", "SRCROOT", "MAKEOBJDIRPREFIX",
    "CROSS_TOOLCHAIN", "WORLDTMP",
    "SUBDIR", "SUBDIRS", "SRCS", "INCS", "MAN",
    # Filenames the writer references as identifiers
    "Makefile", "UPDATING", "COPYRIGHT", "README",
    # Common parameter / variable names that the writer back-ticks but
    # that are not themselves function symbols. They show up as noise
    # when the writer references function signatures like
    # `SYSINIT(name, sub, order, func, udata)` — the args (name, sub,
    # order, func, udata) get pulled as separate "function" candidates.
    "init", "func", "order", "udata", "sid", "name", "sub", "arg",
    "data", "ptr", "ret", "rv", "td", "p", "q", "n", "i", "j", "k",
    # Globals / sentinels frequently mentioned by name but not callable
    "thread0", "proc0", "session0", "pgrp0", "thread0_st", "btext",
    "TDP_NOFAULTING",
    # Generic kernel sysctls / runtime knobs commonly referenced
    "bootverbose", "kdb", "ddb",
    # Linux/macOS structs and funcs the writer occasionally mentions in
    # passing prose (e.g. a one-line analogy in Architecture or Advanced
    # Notes). They don't exist in the FreeBSD tree, but flagging them as
    # "missing" wastes a fact-fix step. The mandatory `## Comparison`
    # section that used to require these names was removed in 2026-05;
    # this denylist remains as cheap insurance against passing references.
    "vm_area_struct", "task_struct", "rw_semaphore", "rwsem",
    "start_kernel", "device_initcall", "core_initcall",
    "postcore_initcall", "module_init", "subsys_initcall",
    # macOS/XNU
    "kernel_bootstrap", "IOKit",
})

# Well-known C macro prefixes that should never be treated as function
# names by the fact-checker. The writer routinely back-ticks these (e.g.
# `SI_SUB_DRIVERS`, `SI_ORDER_FIRST`, `SDT_PROBE_DEFINE`) and they get
# extracted as bare identifiers — but they're #define'd macros, not
# callable functions. Grepping for them in the source tree finds the
# definition fine, but our function-shape filter (`ID *( *ID(`) misses
# them, so they wrongly land in funcs_not_found.
_MACRO_PREFIX_RE = re.compile(
    r'^(?:SI_SUB|SI_ORDER|SYSINIT|SYSUNINIT|SDT_PROBE|TUNABLE|'
    r'CTASSERT|MALLOC_DEFINE|MALLOC_DECLARE|TAILQ|LIST|STAILQ|'
    r'SLIST|RB|MTX|SX|RW|KASSERT|MPASS|VOP|FEATURE|EVENTHANDLER)'
    r'(?:_[A-Z0-9_]+)?$'
)


# Match the `## Comparison` H2 section (and "Comparison with X" variants)
# through to the next H2 or end-of-string. Tokens inside this region
# describe other OSes (Linux, macOS, NetBSD, OpenBSD) and must NOT be
# verified against the FreeBSD source tree — every Linux struct flagged
# as "missing" is a false positive that wastes a fact-fix step.
#
# Legacy-content safety net: the mandatory `## Comparison` section was
# removed from the pipeline in 2026-05. New chapters won't have this
# heading, so the regex is a no-op for them. But chapters previously
# written to disk still carry the section — when those drafts are
# re-fact-checked (e.g. during touch-ups before a regen), this stripper
# keeps the legacy content from generating false positives on Linux/
# macOS symbol names.
_COMPARISON_SECTION_RE = re.compile(
    r'^[ ]{0,3}##\s+Comparison\b.*?(?=^[ ]{0,3}##\s+|\Z)',
    re.MULTILINE | re.DOTALL,
)


def _strip_comparison_section(text: str) -> str:
    """Return `text` with all `## Comparison` H2 sections removed.

    Used by the fact-checker so cross-OS struct/function names in legacy
    chapter content are not grepped against the FreeBSD source tree and
    flagged as missing. New chapters no longer produce this section; this
    function is a no-op for them and only meaningful for on-disk drafts
    written before the section was removed from the pipeline.
    """
    return _COMPARISON_SECTION_RE.sub('', text)


def _filter_known_noise(names: List[str]) -> List[str]:
    """Drop names that match well-known make/config/non-C-symbol patterns."""
    out = []
    for n in names:
        if n in _FACT_CHECK_IGNORE:
            continue
        # All-caps WITH_*, WITHOUT_*, MK_*, NO_* — kernel/make config knobs
        if re.match(r'^(?:WITH|WITHOUT|MK|NO)_[A-Z0-9_]+$', n):
            continue
        # Well-known C macro families (SI_SUB_*, SDT_PROBE_*, TAILQ_*, etc.)
        if _MACRO_PREFIX_RE.match(n):
            continue
        out.append(n)
    return out


def _extract_struct_names(text: str) -> List[str]:
    """Extract claimed struct names from markdown text.

    Two forms are matched:

    1. **Explicit `struct NAME`** — the canonical C form. Always counts.
    2. **Backticked-identifier prose** like ``the `bi_module` structure``
       or ``a `bootinfo` structure``. Bare prose ("the bi_module
       structure") is NOT matched — too noisy with English compounds
       like "a tree structure" / "this data structure". The backticks
       are the writer's signal that the identifier is a real C name.

    ch2 (Boot Process, 2026-05-02) referenced ``a `bi_module`
    structure`` and ``the `bi_modlist` linked list`` for entities that
    don't exist in the FreeBSD tree; the explicit-`struct`-only
    extractor missed both.
    """
    structs = []
    # Form 1: `struct NAME`
    for m in re.finditer(r'\bstruct\s+([a-zA-Z_]\w*)\b', text):
        name = m.group(1)
        if name not in ('struct', 'structs', 'structname'):
            structs.append(name)
    # Form 2: `` `IDENT` structure`` (with the backticks). Identifier
    # must be backticked — bare-word "data structure" / "tree
    # structure" prose would otherwise dominate. Allow "structure",
    # "structs" plural is not interesting (writer uses "structures"),
    # but include singular and plural defensively.
    for m in re.finditer(r'`([a-zA-Z_]\w*)`\s+(?:structure|structures)\b', text):
        name = m.group(1)
        if name not in ('struct', 'structs', 'structname'):
            structs.append(name)
    return _filter_known_noise(list(set(structs)))


def _extract_function_names(text: str) -> List[str]:
    """Extract claimed function names from markdown text.

    Only returns identifiers that have *call evidence* — backticks with
    parentheses (`foo()`), an unbacked "the foo() function" prose
    pattern, or `function-name()` mid-prose. Bare backticked
    identifiers are skipped because they are dominated by struct
    fields, type names, sysctls, parameter names and macro tokens —
    grepping all of those wastes fact-fix steps.

    Also includes function names *defined* inside fenced ```c code
    blocks via `_extract_fenced_function_defs`. The writer sometimes
    plants a fabricated function body as illustrative source ("here is
    `bi_construct(void) { ... }`") — `_extract_function_names`'s
    inline-only patterns miss those entirely, so we union both sets
    before passing to `_verify_functions`. ch2 (Boot Process,
    2026-05-02) shipped with `bi_construct()` for exactly this reason.
    """
    funcs = set()
    # Backtick-quoted function calls: `vm_page_insert()`
    for m in re.finditer(r'`([a-zA-Z_]\w*)\s*\(\s*[^`]*\)`', text):
        funcs.add(m.group(1))
    # "the foo() function" prose pattern
    for m in re.finditer(r'\b(?:the|a|an)\s+([a-zA-Z_]\w*)\s*\(\s*\)\s+function', text):
        funcs.add(m.group(1))
    # "foo() function" / "function foo()"
    for m in re.finditer(r'\b([a-zA-Z_]\w*)\s*\(\s*\)\s+function\b', text):
        funcs.add(m.group(1))
    for m in re.finditer(r'\bfunction\s+([a-zA-Z_]\w*)\s*\(\s*\)', text):
        funcs.add(m.group(1))
    # "calls foo()" / "invokes foo()"
    for m in re.finditer(r'\b(?:calls?|invokes?|returns? from)\s+`?([a-zA-Z_]\w*)`?\s*\(\s*\)', text):
        funcs.add(m.group(1))
    # Function definitions inside fenced ```c blocks.
    funcs.update(_extract_fenced_function_defs(text))
    return _filter_known_noise(list(funcs))


# Match a function definition inside a fenced C code block: a return-
# type chunk, then NAME(...) followed by `{` (not `;`). Anchored to a
# line start so member-access expressions like `bi->bi_efi_memmap = (void
# *)(bi + 1);` can never be mistaken for definitions, and so call sites
# inside larger expressions are skipped.
#
# Allows K&R style where the return type sits on the previous line:
#     static int
#     bi_construct(void)
#     {
# `(?:[A-Za-z_]\w*\s*\**\s+)+` matches one-or-more type/qualifier
# tokens with optional `*`s; `\*?\s*` then absorbs a trailing pointer
# next to the function name. The arg list `\([^;{]*?\)` may span lines
# (DOTALL) but cannot contain `;` or `{`. A trailing `\s*\{` requires
# the body-opening brace, which is what distinguishes a definition from
# a prototype.
# Wall-clock ceiling for the fenced-block scan in
# _extract_fenced_function_defs. Generous: the whole scan is ~0.001s on a
# 20KB draft once the regex is linear, so tripping this means something
# is wrong, not merely large.
_FENCED_SCAN_BUDGET_SEC = 20.0

# The type-token group is ATOMIC and BOUNDED, and both matter.
#
# The original `(?:[A-Za-z_]\w*\s*\**\s+)+` backtracked catastrophically:
# it is ambiguous with the `\*?\s*` that follows (both can absorb the
# pointer stars and the whitespace), so when the trailing `\{` fails to
# match the engine re-splits the same run of type tokens exponentially
# many ways. On a real 20KB ch3 draft this never returned — the process
# spun at 99% CPU for 6.8 hours with the LLM endpoint completely idle,
# holding a chapter hostage after the writer had already finished it
# (2026-08-22). Every other extractor on the same draft ran in <0.1s.
#
# `(?>...)` commits to the longest match of each type token and never
# re-divides it, which removes the exponential blowup. `{1,8}` bounds
# the worst case regardless: no real C declaration needs more than a
# handful of type/qualifier tokens ("static const struct foo *" is 4).
#
# Keep both. The atomic group is the actual fix; the bound is the
# seatbelt for whatever ambiguity a future edit reintroduces.
_FENCED_FUNC_DEF_RE = re.compile(
    r"^"
    r"(?>(?:[A-Za-z_]\w*\s*\**\s+){1,8})"
    r"\*?\s*([A-Za-z_]\w*)\s*"
    r"\([^;{]*?\)\s*"
    r"(?:__\w+(?:\s*\([^)]*\))?\s*)*"
    r"\{",
    re.MULTILINE | re.DOTALL,
)


def _extract_fenced_function_defs(text: str) -> List[str]:
    """Find function definitions inside fenced ```c code blocks.

    Returns identifiers that look like the *defined* function (the one
    whose body opens immediately after the signature). A code block
    that only *calls* `foo()` does not produce a hit — the
    body-opening `{` is what gates the match.

    Why this exists: the writer can plant a fabricated function body
    as illustrative source — `static int bi_construct(void) { ... }` —
    and the inline-call extractors in `_extract_function_names` won't
    touch it. ch2 shipped with `bi_construct()` (does not exist in the
    tree) for precisely this reason.
    """
    found: set = set()
    # Wall-clock budget across all fenced blocks. _FENCED_FUNC_DEF_RE was
    # fixed (see its atomic group) after backtracking exponentially on a
    # real draft and spinning one CPU for 6.8 hours with no log output —
    # the chapter was already written, and the pipeline simply never came
    # back. re.finditer cannot be interrupted from Python, so this budget
    # cannot stop a single pathological match; what it does is stop the
    # *loop* from compounding across blocks and make the failure visible
    # in the log instead of silent. Anything slow enough to trip this is
    # a bug worth seeing.
    deadline = time.monotonic() + _FENCED_SCAN_BUDGET_SEC
    for block in _FENCED_BLOCK_RE.finditer(text):
        if time.monotonic() > deadline:
            print(f"  [fact-check] warning: fenced-function scan exceeded "
                  f"{_FENCED_SCAN_BUDGET_SEC}s — remaining ```c blocks "
                  f"skipped (report this; the regex should be linear)")
            break
        body = block.group(1)
        # Strip C comments so a name in `/* foo() */` doesn't promote
        # to a definition claim.
        body = _strip_c_comments(body)
        for m in _FENCED_FUNC_DEF_RE.finditer(body):
            name = m.group(1)
            # `if`/`while`/`for`/`switch` etc. match the same shape;
            # filter via the existing noise list which already covers
            # C keywords.
            found.add(name)
    return _filter_known_noise(list(found))


def _verify_file_paths(paths: List[str], src_root: str) -> List[str]:
    """Verify that claimed file paths exist in the source tree.

    Returns a list of "path: not found" strings for missing files.
    Also tries a glob fallback for close matches and emits a "wrong → right"
    correction — but only when the proposed correction is itself a real
    file, so we never suggest the writer rewrite to a path that also
    doesn't exist.
    """
    not_found = []
    for p in paths:
        full = os.path.join(src_root, p)
        if os.path.exists(full):
            continue
        # Paths written relative to /usr/src: "src/bin/cp" is "<src>/bin/cp".
        # Common in layout prose and not a hallucination.
        if p.startswith('src/') and os.path.exists(
                os.path.join(src_root, p[len('src/'):])):
            continue
        # Kernel-relative paths. Chapters about kernel subsystems write
        # include paths the way C does — `sys/proc.h` for <sys/proc.h>
        # (really sys/sys/proc.h), `vm/uma.h`, `netinet/ip_fw.h` — and
        # bare directory names like "kern" or "amd64" for sys/kern and
        # sys/amd64. All are correct prose. Flagging them would burn
        # fact-fix steps on accurate text, the failure mode
        # _FACT_CHECK_IGNORE exists to prevent, and the glob fallback
        # below makes it worse by "correcting" sys/proc.h to an
        # arch-specific sys/powerpc/include/proc.h.
        # Note this exemption necessarily swallows a real error class: a
        # claim about a retired TOP-LEVEL directory whose name still exists
        # under sys/ (bare `gnu` vs the retired top-level `gnu/`) resolves
        # to sys/gnu and passes. Using the writer's trailing slash to tell
        # the two apart was tried and reverted — chapters write `kern/`,
        # `vm/`, `amd64/` with slashes too, so it turned ~20 correct
        # kernel-relative claims into false positives to catch one real
        # one. Better to miss that case than to spend the writer's
        # fact-fix budget rewriting accurate prose.
        if any(os.path.exists(os.path.join(src_root, parent, p))
               for parent in ('sys', 'sys/dev', 'include', 'lib',
                              'share', 'usr.bin', 'usr.sbin')):
            continue
        # `machine/foo.h` is the per-architecture include alias: at build
        # time machine/ points at sys/<arch>/include/. It never exists as
        # a literal path, so resolve it against the arch directories
        # instead of reporting a real header as fabricated.
        if p.startswith('machine/'):
            leaf = p[len('machine/'):]
            if glob.glob(os.path.join(src_root, 'sys', '*', 'include', leaf)):
                continue
        # Try glob fallback
        pattern = os.path.join(src_root, p.split('/')[0], '**', p.split('/')[-1])
        matches = list(glob.glob(pattern, recursive=True))
        # Filter to candidates that actually exist on disk. Glob already
        # returns existing entries, but be defensive against symlinks or
        # races between glob and the os.path.exists() recheck below.
        # isfile would reject directory corrections, which this function
        # now also verifies — use a plain existence check.
        # Require the correction to share the claimed path's parent
        # directory name, not merely its basename. Matching on basename
        # alone across the whole tree produces confident nonsense —
        # `sys/conf/config` "corrected" to `sys/contrib/openzfs/config`,
        # which sends the writer to rewrite correct-ish prose into a
        # genuinely unrelated file. A wrong correction is worse than no
        # correction: the writer trusts it.
        want_parent = os.path.basename(os.path.dirname(p)) if '/' in p else ''
        candidate = next(
            (m for m in matches
             if os.path.exists(m)
             and (not want_parent
                  or os.path.basename(os.path.dirname(m)) == want_parent)),
            None,
        )
        if candidate is not None:
            not_found.append(f"{p} → {os.path.relpath(candidate, src_root)}")
        else:
            not_found.append(p)
    return not_found


# Per-process memo of fact-check results, keyed by (kind, src_root, symbol).
# Survives across revision rounds within a single run, so a struct verified
# during the first pass is not re-grepped during fact-fix or subsequent revisions.
_FACT_CHECK_CACHE: Dict[Tuple[str, str, str], bool] = {}

# Single-call timeout for the batched grep. The previous code spent up to
# 5 s per symbol; one batched grep with -m1 short-circuiting per file is
# fast even on the full sys/ tree, so a tighter timeout is safe and bounds
# the total cost. Bumped 8→30 on 2026-05-03 after ch13 (ZFS) timed out:
# the 55MB openzfs contrib subtree (~1100 C files) can't be batched-grepped
# in 8s. 30s is still a hard ceiling — at that point the symbols are
# treated as unverified, which cascades into reviewer accuracy:FAIL.
_GREP_TIMEOUT_SEC = 30

# Corpus-level grep-output cache for the small, near-static macro corpora
# (kernel-config options, SDT probes). Unlike the per-symbol
# `_FACT_CHECK_CACHE`, these checks match every candidate against the SAME
# grep output, so caching the output once per (kind, root) turns N grabs —
# one per revision round — into a single grep for the whole run. Keyed by
# an opaque string the caller builds; the value is the captured stdout.
# A timeout is never stored (fail-closed, must be retryable next round).
_GREP_CORPUS_CACHE: Dict[str, str] = {}


def _cached_grep_corpus(cache_key: str, cmd: str) -> Optional[str]:
    """Run `cmd` once per `cache_key`, caching stdout for the run.

    Returns the captured stdout, or `None` on timeout. `None` is NOT
    cached: the timeout is transient, so the next round retries. This is
    the fail-closed policy shared with `_batched_grep_present` — a caller
    that gets `None` must flag every candidate as unverified rather than
    silently approving it.
    """
    if cache_key in _GREP_CORPUS_CACHE:
        return _GREP_CORPUS_CACHE[cache_key]
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            errors='replace', timeout=_GREP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print(f"  ⚠ fact-check grep timed out after {_GREP_TIMEOUT_SEC}s "
              f"({cache_key}) — treating candidates as unverified")
        return None
    _GREP_CORPUS_CACHE[cache_key] = result.stdout
    return result.stdout


def _batched_grep_present(symbols: List[str], pattern_template: str,
                          search_roots, shape_grep: str) -> Optional[set]:
    """Run one grep over `search_roots` looking for any of `symbols`.

    `search_roots` is a string (single root) or a sequence of strings
    (multiple roots, all OR'd in a single grep invocation). Multiple
    roots are how chapters opt into searching outside `sys/` — a boot
    chapter declares `extra_search_dirs: ["stand"]` so symbols that
    legitimately live under `stand/efi/` (e.g. `EFI_MEMORY_DESCRIPTOR`,
    `preloaded_file`) are not falsely flagged as missing.

    Two-stage pipeline:
      1. `grep -Fw` (fixed-strings, word-boundaried) for the candidate
         symbols. BSD `grep -E` is pathologically slow with nested
         alternations like `(void|int|...) (a|b|...)` (40 s+ on sys/);
         `grep -F` with multiple `-e` patterns runs in well under a
         second on the same tree.
      2. A second `grep -E` filters those lines down to candidate
         *definitions* (e.g. `struct ... {` or `... \\(`) so the 1 MB
         output cap captures definitions rather than the dense forest
         of pointer-typed uses (`struct vm_page **ma, ...`) that would
         otherwise dominate.
      3. Per-symbol Python regex (`pattern_template`) re-scans the
         captured output to confirm the shape per symbol.

    `pattern_template` is consumed by the Python re re-scan, not by
    grep. `shape_grep` is the BSD-grep-friendly shape filter for stage 2
    (no `\\s`, no nested alternations).

    Returns the set of symbols that passed both stages. Symbols that did
    not match — or all symbols, if grep itself fails or times out — are
    absent from the returned set.
    """
    if not symbols:
        return set()

    if isinstance(search_roots, str):
        roots = [search_roots]
    else:
        roots = [r for r in search_roots if r]
    if not roots:
        return set()

    # Stage 1: fast fixed-string grep for any of the symbols. `-w` keeps
    # us from matching substrings (e.g. `proc` inside `procfs`).
    # Stage 2: shape filter so the 1 MB cap holds candidate definitions.
    fixed_args = " ".join(f"-e {shlex.quote(s)}" for s in symbols)
    roots_arg = " ".join(f"{shlex.quote(r)}/" for r in roots)
    cmd = (
        f"grep -rhwF --include='*.c' --include='*.h' {fixed_args} "
        f"{roots_arg} 2>/dev/null | "
        f"grep -E {shlex.quote(shape_grep)} | "
        f"head -c 1048576"
    )
    try:
        # `errors='replace'` is load-bearing: FreeBSD source contains a
        # few non-UTF8 bytes (Latin-1 author names in old driver
        # comments). Without an error policy, Python's text-mode
        # subprocess decode raises UnicodeDecodeError mid-pipeline and
        # the whole run aborts before atomic write. ch14 (sys/net)
        # hit this on 2026-04-30 — one bad byte killed an hour of work.
        # The same policy is applied to every other grep-over-tree
        # subprocess.run() in this file for the same reason.
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            errors='replace', timeout=_GREP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        # Conservative (fail-closed) behaviour: treat a timeout as
        # "verification not available" so callers report all symbols as
        # not-found rather than silently approving them. A silent timeout
        # would mask real hallucinations, the failure mode we care most
        # about. We return `None` (NOT an empty set) so the caller can
        # tell "grep ran, nothing matched" (cacheable) apart from "grep
        # timed out" (must NOT be cached — a transient timeout would
        # otherwise poison the cache and mark these symbols missing for
        # the rest of the run).
        print(f"  ⚠ fact-check grep timed out after {_GREP_TIMEOUT_SEC}s — "
              f"treating {len(symbols)} symbol(s) as unverified")
        return None

    output = result.stdout
    if not output:
        return set()

    # Stage 2: validate the shape per symbol with Python re. MULTILINE
    # so `^NAME\s*\(` patterns (K&R-style function defs with the name
    # at column 0) match per-line in the multi-line grep output, not
    # just at byte 0.
    matched = set()
    for s in symbols:
        py_pattern = pattern_template.format(alt=re.escape(s))
        if re.search(py_pattern, output, re.MULTILINE):
            matched.add(s)
    return matched


def _resolve_search_roots(src_root: str,
                          extra_search_dirs: Optional[List[str]] = None
                          ) -> Tuple[List[str], str]:
    """Build the list of grep roots and a stable cache-key suffix.

    Always includes `<src_root>/sys`. Additionally includes each entry
    in `extra_search_dirs`, joined with `src_root` if relative, taken
    as-is if absolute. Non-existent roots are silently dropped (a
    chapter opting into `stand` on a tree without `stand/` is a no-op,
    not an error).

    The suffix is the sorted-tuple of *extra* roots only (the `sys`
    root is implicit), joined with `|`. It lands in the cache key so
    `(struct, sys-only)` and `(struct, sys+stand)` don't alias.
    """
    roots = [os.path.join(src_root, "sys")]
    extras: List[str] = []
    for d in extra_search_dirs or []:
        if not d:
            continue
        path = d if os.path.isabs(d) else os.path.join(src_root, d)
        if os.path.isdir(path):
            roots.append(path)
            extras.append(path)
    cache_suffix = "|".join(sorted(extras))
    # Filter to roots that actually exist on disk.
    roots = [r for r in roots if os.path.isdir(r)]
    return roots, cache_suffix


def _verify_with_cache(kind: str, symbols: List[str], src_root: str,
                       pattern_template: str, shape_grep: str,
                       extra_search_dirs: Optional[List[str]] = None
                       ) -> List[str]:
    """Common path for struct/function verification.

    Splits `symbols` into already-cached and uncached, runs one batched
    grep over the uncached set, updates the cache, then returns the list
    of symbols that are not present in the source tree.

    `extra_search_dirs` widens the grep beyond `sys/` for chapters that
    legitimately discuss code under `stand/`, `lib/`, `usr.bin/`, etc.
    The cache key includes the extra dirs so a symbol verified under a
    widened search isn't reused for a chapter using only `sys/`.
    """
    search_roots, cache_suffix = _resolve_search_roots(
        src_root, extra_search_dirs,
    )
    cache_root = src_root + ("::" + cache_suffix if cache_suffix else "")
    uncached = []
    not_found = []
    for s in symbols:
        cached = _FACT_CHECK_CACHE.get((kind, cache_root, s))
        if cached is True:
            continue
        if cached is False:
            not_found.append(s)
            continue
        uncached.append(s)

    if uncached:
        present = _batched_grep_present(
            uncached, pattern_template, search_roots, shape_grep,
        )
        if present is None:
            # Grep timed out. Fail closed — flag every uncached symbol as
            # not-found this round — but do NOT cache the verdict: the
            # timeout is transient and caching `False` would keep these
            # symbols flagged for every later round in the run.
            not_found.extend(uncached)
        else:
            for s in uncached:
                present_now = s in present
                _FACT_CHECK_CACHE[(kind, cache_root, s)] = present_now
                if not present_now:
                    not_found.append(s)

    return not_found


def _verify_structs(structs: List[str], src_root: str,
                    extra_search_dirs: Optional[List[str]] = None
                    ) -> List[str]:
    """Verify that claimed struct names exist in the source tree.

    Returns a list of struct names that could not be found.
    Backed by `_FACT_CHECK_CACHE` so re-runs within a session are free.

    `extra_search_dirs` is forwarded to `_verify_with_cache` — a boot
    chapter passes `["stand"]` so `EFI_MEMORY_DESCRIPTOR`, `preloaded_file`,
    etc. verify against the bootloader tree instead of being flagged.
    """
    # Match the canonical struct definition shape. Two alternatives,
    # both of which appear in real FreeBSD code:
    #
    #   1. Same-line brace:  `struct NAME {`
    #   2. K&R-style brace on next line:
    #          struct NAME
    #          {
    #      `bootstrap.h:230` — `struct preloaded_file\n{` — is the
    #      example that motivated this. Without the K&R alternative,
    #      `_verify_structs` reports real `stand/` structs as missing
    #      and the reviewer downgrades accuracy on chapters that
    #      legitimately discuss them.
    #
    # `pattern_template` is for Python re (per-symbol re-scan over the
    # grep output); `shape_grep` is the BSD-grep filter that keeps
    # only candidate-definition lines so the 1 MB cap holds them.
    # Forward decls (`struct foo;`) and pointer uses (`struct foo *p`)
    # are intentionally NOT matched by either alternative.
    return _verify_with_cache(
        "struct", structs, src_root,
        pattern_template=r"struct\s+({alt})(?:\s*\{{|\s*$)",
        shape_grep=(
            r"^struct [A-Za-z_][A-Za-z0-9_]* *\{|"
            r"^struct [A-Za-z_][A-Za-z0-9_]* *$"
        ),
        extra_search_dirs=extra_search_dirs,
    )


def _verify_functions(funcs: List[str], src_root: str,
                      extra_search_dirs: Optional[List[str]] = None
                      ) -> List[str]:
    """Verify that claimed function names exist in the source tree.

    Returns a list of function names that could not be found.
    Backed by `_FACT_CHECK_CACHE` so re-runs within a session are free.

    `extra_search_dirs` widens the grep beyond `sys/`. ch2 (Boot
    Process) needs `["stand"]` so `elf64_exec`, `bi_load`, and other
    bootloader-internal functions are not falsely flagged as missing.
    """
    # The Python re re-scan needs to confirm "this line really is a
    # function definition shape" for the symbol. Earlier versions
    # enumerated return-type tokens (void|int|struct|enum|...) and
    # required them directly adjacent to the name, which falsely
    # rejected everything written K&R-style across multiple words:
    #
    #   struct inpcb *
    #   in_pcblookup_hash(...)
    #   {
    #
    # Here `struct inpcb *` is two words plus a star; the old
    # `(?:struct|...)\s+\*?\s*NAME` pattern wanted `struct *NAME` and
    # missed the real definition. Same root cause that caused
    # _dirmap_extract_names to under-count symbols. The shape we
    # actually want is: at least one identifier-token chunk (the
    # return type / qualifiers), then NAME, then `(`. The grep stage
    # already restricted to lines containing the symbol, so the
    # Python pass just needs to confirm we're looking at a defn-shape
    # line and not a call site or struct field.
    pattern = (
        r"(?:"
        # Single-line return-type form: `void *foo(`, `static int foo(`,
        # `struct inpcb *foo(` — at least one identifier-token before
        # NAME. The grep stage filters call sites; this just confirms
        # we're seeing a defn-shape and not a struct-field reference.
        r"(?:[A-Za-z_]\w*\s*\**\s+)+\*?\s*({alt})\s*\("
        r"|"
        # K&R-style with name at column 0 — return type lives on the
        # previous line:
        #     struct inpcb *
        #     in_pcblookup_hash(struct in_addr, u_short, ...)
        # This is how a lot of FreeBSD network code is written. The
        # shape grep accepts these via the `^NAME\s*\(` alternative,
        # and we accept them here too.
        r"^({alt})\s*\("
        r")"
    )
    # `shape_grep` keeps lines that look like a function signature.
    # Two alternatives:
    #   1. `ID *? ID(` — covers `void malloc(` / `void *malloc(`-style
    #      single-line defs and prevents call sites (`malloc(...)`)
    #      from leaking through.
    #   2. `^ID(` — covers K&R-style defs where the name sits at
    #      column 0 because the return type is on the previous line.
    #      Without this, `tcp_newtcpcb(struct inpcb *, ...)` lines are
    #      filtered out and the symbol is wrongly reported missing.
    return _verify_with_cache(
        "func", funcs, src_root,
        pattern_template=pattern,
        shape_grep=(
            r"[A-Za-z_][A-Za-z0-9_]* *\*? *[A-Za-z_][A-Za-z0-9_]*\("
            r"|^[A-Za-z_][A-Za-z0-9_]*\("
        ),
        extra_search_dirs=extra_search_dirs,
    )


# --- Kernel-config options -------------------------------------------------
#
# Writers occasionally invent kernel-config options like `VERBOSE_SYSINIT`
# that look plausible but don't exist. Real options live in `sys/conf/options`
# and per-arch `sys/conf/options.<arch>` files, plus `sys/conf/NOTES` (which
# documents `option FOO`). We extract candidate names from the draft and
# grep those files; anything not found is reported back to the writer.

# Match `option FOO` mentions (with optional backticks) and bare ALL_CAPS
# tokens introduced as kernel options. Restricted to identifiers of length
# ≥ 5 to avoid matching incidental ALL-CAPS words like `BSD` or `API`.
_KERNEL_OPTION_RE = re.compile(
    r'\b(?:option|options)\s+`?([A-Z][A-Z0-9_]{4,})`?\b'
    r'|`(VERBOSE_[A-Z0-9_]+|DEBUG_[A-Z0-9_]+|INVARIANT[A-Z0-9_]*|'
    r'WITNESS[A-Z0-9_]*|KTR[A-Z0-9_]*|BOOTVERBOSE)`'
)

# Tokens that look like kernel-config options but are universally known
# kernel knobs and not worth fact-checking (false positives if missing
# from this particular tree, e.g. arch-specific options).
_KERNEL_OPTION_IGNORE = frozenset({
    "BOOTVERBOSE",  # actually a sysctl/loader var, not an `option`
})


def _extract_kernel_options(text: str) -> List[str]:
    """Extract claimed kernel-config option names from the draft."""
    found = set()
    for m in _KERNEL_OPTION_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if name and name not in _KERNEL_OPTION_IGNORE:
            found.add(name)
    return sorted(found)


def _verify_kernel_options(options: List[str], src_root: str) -> List[str]:
    """Grep `sys/conf/options*` and `sys/conf/NOTES` for each option name.

    Returns the list of options that were not found.
    """
    if not options:
        return []
    sys_conf = os.path.join(src_root, "sys", "conf")
    if not os.path.isdir(sys_conf):
        return []  # can't verify — don't flag
    # The option corpus (sys/conf/options*, NOTES) is the same regardless
    # of which options we're checking, so grep it once per run and match
    # every candidate against the cached output. `-w` on any option name
    # keeps the grep cheap; we widen the match set to all options here so
    # the cache key is stable across revision rounds that check different
    # subsets.
    cmd = (
        f"grep -rh "
        f"--include='options' --include='options.*' --include='NOTES' "
        f"-E '[A-Z][A-Z0-9_]{{3,}}' {shlex.quote(sys_conf)}/ 2>/dev/null | "
        f"head -c 524288"
    )
    output = _cached_grep_corpus(f"kernopt::{sys_conf}", cmd)
    if output is None:
        # Fail closed: grep timed out, flag all candidates as unverified.
        return list(options)
    not_found = []
    for opt in options:
        # Word-boundary check; `re.search` is fine since the corpus is small.
        if not re.search(rf'\b{re.escape(opt)}\b', output):
            not_found.append(opt)
    return not_found


# --- DTrace SDT probes -----------------------------------------------------
#
# Writers also invent SDT probes like `sysinit:::entry` or
# `vm:::page-fault`. Real probes are registered with `SDT_PROBE_DEFINE*`
# macros in `sys/`. We extract `provider:module:function:name` tuples from
# the draft and verify each by matching `SDT_PROBE_DEFINE…(provider, …,
# name)` patterns. The middle two fields (module/function) are usually
# empty in writer-output (`sysinit:::entry`), so we only verify provider+name.

_DTRACE_PROBE_RE = re.compile(
    r'`([a-z][a-z0-9_-]*):[a-z0-9_-]*:[a-z0-9_-]*:([a-z0-9_-]+)`'
)


def _extract_dtrace_probes(text: str) -> List[Tuple[str, str]]:
    """Extract claimed DTrace SDT probes as (provider, name) tuples."""
    return list({
        (m.group(1), m.group(2)) for m in _DTRACE_PROBE_RE.finditer(text)
    })


def _verify_dtrace_probes(probes: List[Tuple[str, str]],
                          src_root: str) -> List[str]:
    """Grep for SDT_PROBE_DEFINE* macros matching each (provider, name) tuple.

    Returns a list of "provider:::name" strings that were not found.
    """
    if not probes:
        return []
    sys_root = os.path.join(src_root, "sys")
    if not os.path.isdir(sys_root):
        return []
    # Pull all SDT_PROBE_DEFINE* lines from sys/. The grep is cheap because
    # the macro is rare. The 1 MB cap is generous; the FreeBSD tree has a
    # few hundred SDT probes total. The corpus is identical regardless of
    # which probes we check, so cache it once per run rather than grepping
    # all of sys/ on every revision round.
    cmd = (
        f"grep -rhE --include='*.c' --include='*.h' "
        f"'SDT_PROBE_DEFINE[0-9]?\\b' {shlex.quote(sys_root)}/ 2>/dev/null | "
        f"head -c 1048576"
    )
    output = _cached_grep_corpus(f"sdtprobe::{sys_root}", cmd)
    if output is None:
        # Fail closed: grep timed out, flag all candidates as unverified.
        return [f"{p}:::{n}" for p, n in probes]
    not_found = []
    for provider, name in probes:
        # Match `SDT_PROBE_DEFINE…(provider, anything, anything, name`
        pat = (
            rf'SDT_PROBE_DEFINE\d?\s*\(\s*{re.escape(provider)}\s*,'
            rf'[^)]*?\b{re.escape(name)}\b'
        )
        if not re.search(pat, output):
            not_found.append(f"{provider}:::{name}")
    return not_found


# --- MALLOC_DEFINE / MALLOC_DECLARE tags -----------------------------------
#
# Writers reach for `M_FOO` malloc tags as a way to make a section feel
# concrete ("allocations are tracked under `M_PROC`"), and they invent
# tags as readily as they invent struct names. Real tags are introduced
# by `MALLOC_DEFINE(M_FOO, "name", "desc");` and shared via
# `MALLOC_DECLARE(M_FOO);` in headers. We extract `M_*` tokens from the
# draft and grep `sys/` for either macro mentioning that token.
#
# The token regex insists on `M_` followed by ≥2 uppercase chars to
# avoid collecting one-letter macros (`M_PI`, etc.) and ALL-CAPS prose
# fragments. We further filter out a handful of tokens that look like
# tags but aren't (mbuf flags, network mask constants).

_MALLOC_TAG_RE = re.compile(r'`(M_[A-Z][A-Z0-9_]{1,})`')

# Common `M_FOO` tokens that are NOT malloc tags — mbuf flags, network
# mask constants, generic option markers. Verifying these against
# MALLOC_DEFINE will always fail and waste fact-fix steps on real text.
_MALLOC_TAG_IGNORE = frozenset({
    "M_NOWAIT", "M_WAITOK", "M_ZERO", "M_NODUMP", "M_USE_RESERVE",
    "M_NOVM", "M_BESTFIT", "M_FIRSTFIT", "M_EXEC", "M_CONTIG",
    "M_PKTHDR", "M_EOR", "M_MCAST", "M_BCAST", "M_FRAG", "M_LASTFRAG",
    "M_PROTO1", "M_PROTO2", "M_PROTO3", "M_PROTO4", "M_PROTO5",
    "M_VLANTAG", "M_PROMISC", "M_NOFREE", "M_EXT", "M_HASHTYPE_OPAQUE",
})


def _extract_malloc_tags(text: str) -> List[str]:
    """Extract claimed MALLOC_DEFINE tag names (`M_FOO`) from the draft."""
    found = set()
    for m in _MALLOC_TAG_RE.finditer(text):
        tag = m.group(1)
        if tag and tag not in _MALLOC_TAG_IGNORE:
            found.add(tag)
    return sorted(found)


def _verify_malloc_tags(tags: List[str], src_root: str) -> List[str]:
    """Verify claimed `M_FOO` malloc tags exist in `sys/`.

    A tag is real if some `MALLOC_DEFINE(M_FOO, ...)` or
    `MALLOC_DECLARE(M_FOO)` macro references it. Backed by the shared
    `_FACT_CHECK_CACHE` (kind='malloc') so re-runs are free.
    """
    # `pattern_template` is the Python re re-scan; `shape_grep` is the
    # BSD-grep stage-2 filter that keeps only candidate macro lines so
    # the 1 MB output cap holds them.
    return _verify_with_cache(
        "malloc", tags, src_root,
        pattern_template=r"MALLOC_(?:DEFINE|DECLARE)\s*\(\s*({alt})\b",
        shape_grep=r"MALLOC_(DEFINE|DECLARE)\(",
    )


# --- Sysctl OID paths (graph-backed, optional) -----------------------------
#
# THE ONE SYMBOL CLASS GREP CANNOT VERIFY. A sysctl OID path such as
# `vm.pmap.pde.mappings` exists nowhere in the source as a literal string —
# it is assembled at compile time from a chain of SYSCTL_* macros:
#
#     SYSCTL_NODE(_vm,       OID_AUTO, pmap, ...)            -> vm.pmap
#     SYSCTL_NODE(_vm_pmap,  OID_AUTO, pde,  ...)            -> vm.pmap.pde
#     SYSCTL_COUNTER_U64(_vm_pmap_pde, OID_AUTO, mappings, ..)-> vm.pmap.pde.mappings
#
# The leaf (`mappings`) is a bare identifier token, and the prefix comes
# from walking the `_vm_pmap_pde` parent chain. `grep vm.pmap.pde.mappings`
# returns nothing for a REAL sysctl exactly as it does for a fabricated one,
# so grep cannot tell them apart — and sysctls are a favorite writer
# hallucination ("tune `net.inet.tcp.fictional_knob`").
#
# codebase-memory-mcp reconstructs the OID tree during indexing and stores
# each resolved path as a `Sysctl` graph node. We verify a claimed OID with
# an anchored `search_graph` name-pattern query against that label:
# real -> 1 hit at the registration line, fabricated -> 0 hits.
#
# GRACEFUL DEGRADATION: this is the only verifier with an external backend.
# When the graph is unavailable (`_sysctl_graph_available()` is False) it
# returns [] — nothing flagged — after the probe's one-time warning. The
# rest of fact-check is unaffected.
#
# FALSE-NEGATIVE, NOT FALSE-POSITIVE: ~1/3 of Sysctl nodes are `resolved:
# false` (parent chain built from a macro arg / runtime string the indexer
# could not resolve). Those never match a real dotted query, so a genuine
# OID among them would be reported "not found". That is the SAFE direction
# for a hallucination checker (over-flag, never under-flag), and it is why
# the fact-fix prompt says "verify against sysctl(8)", NOT "hallucinated".

# Canonical top-level OID namespaces (covers 99%+ of resolved OIDs plus the
# `compat.*` tree). Scoping to these avoids matching arbitrary dotted prose
# — method calls (`obj.method`), version strings, filenames — as sysctls.
_SYSCTL_ROOTS = (
    "kern", "vm", "net", "hw", "dev", "vfs", "debug", "security",
    "machdep", "user", "compat", "kstat", "p1003_1b",
)

# Backticked OID: a canonical root followed by at least one `.segment`.
# Requires >=2 segments total so a bare `kern` or `hw` never matches.
_SYSCTL_OID_RE = re.compile(
    r'`(' + r'|'.join(_SYSCTL_ROOTS) + r')((?:\.[a-z0-9_]+)+)`'
)

# Per-OID availability cache (True=real, False=not found). Separate from the
# grep `_FACT_CHECK_CACHE` because the backend and key space are different.
_SYSCTL_GRAPH_CACHE: Dict[str, bool] = {}


def _extract_sysctls(text: str) -> List[str]:
    """Extract claimed sysctl OID paths (`kern.ipc.maxsockbuf`) from a draft.

    Only backticked tokens whose first segment is a canonical sysctl root
    AND that have at least two dotted segments are collected — this keeps
    ordinary dotted prose (method calls, filenames, version numbers) out of
    the candidate set. Deduplicated, sorted.
    """
    found = set()
    for m in _SYSCTL_OID_RE.finditer(text):
        found.add(m.group(1) + m.group(2))
    return sorted(found)


def _sysctl_exists_in_graph(oid: str) -> Optional[bool]:
    """Query the graph for one sysctl OID. True/False, or None on error.

    Runs `codebase-memory-mcp cli search_graph` with an ANCHORED name
    pattern (`^oid$`) restricted to the `Sysctl` label, and reads the
    `total:` count from the wrapped tree-text payload. Returns None (not
    False) on any subprocess/parse failure so the caller can distinguish
    "verified absent" from "could not check" and avoid false-flagging.
    """
    # Anchor the regex; escape the dots so they match literally rather than
    # any-char (an unescaped `kern.ipc` would also match `kernXipc`).
    pattern = "^" + re.escape(oid) + "$"
    try:
        proc = subprocess.run(
            [SYSCTL_GRAPH["bin"], "cli", "--json", "search_graph",
             "--project", SYSCTL_GRAPH["project"],
             "--name-pattern", pattern, "--label", "Sysctl", "--limit", "1"],
            capture_output=True, text=True, errors="replace",
            timeout=SYSCTL_GRAPH["timeout"],
        )
    except Exception:
        return None

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("isError"):
            return None
        text = ""
        if isinstance(payload.get("content"), list) and payload["content"]:
            text = payload["content"][0].get("text", "")
        m = re.search(r"total:\s*(\d+)", text)
        if m:
            return int(m.group(1)) > 0
    return None


def _verify_sysctls_via_graph(oids: List[str]) -> List[str]:
    """Return the subset of `oids` NOT found as resolved sysctl nodes.

    No-op (returns []) when the graph backend is unavailable — the "graph
    optional" contract. OIDs whose lookup errors out (None) are treated as
    present, never flagged: we only report an OID as suspect when the graph
    positively returns zero matches. Results are cached per OID.
    """
    if not oids or not _sysctl_graph_available():
        return []
    missing = []
    for oid in oids:
        cached = _SYSCTL_GRAPH_CACHE.get(oid)
        if cached is None:
            exists = _sysctl_exists_in_graph(oid)
            # None (lookup failed) -> treat as present so a flaky backend
            # never manufactures a "hallucinated sysctl" finding.
            cached = True if exists is None else exists
            _SYSCTL_GRAPH_CACHE[oid] = cached
        if not cached:
            missing.append(oid)
    return missing


# --- Undefined jargon (first-use gloss check) ------------------------------
#
# THE PROBLEM THIS SOLVES. Every other verifier in this file asks "is this
# symbol real?". This one asks "will a junior reader understand this
# sentence?" — a different failure mode entirely, and one the reviewer
# rubric demonstrably does not catch on its own.
#
# The canonical example (ch12, buffer cache, 2026-08-27):
#
#     "The `BIO_UNMAPPED` flag indicates that the data pages are not
#      mapped into KVM and must be mapped before the I/O can proceed."
#
# `KVM` appears five times in that chapter and is never defined. Worse, the
# chapter then invented a reason for the two modes ("because the pageout
# daemon has paged them out" — wrong; unmapped I/O exists so a device can
# DMA straight from physical pages without spending KVM and pmap/TLB work).
# Undefined jargon and confabulated rationale are the SAME defect: the
# writer treated "the flag exists" as sufficient and never engaged with why.
#
# Reviewer criterion 8 ("Rationale") already asks for the why, and passed
# this chapter anyway. Three structural reasons, all of which argue for
# doing the check in Python instead of asking the model harder:
#   1. Its trigger examples are VM-flavoured (shadow chains, UMA kegs, NUMA
#      domains) so a *flag* never reads as a "non-obvious mechanism".
#   2. It is one binary verdict over a ~25 KB draft: find rationale for
#      three big things and five undefined terms ride along.
#   3. It asks the model to simulate a junior reader. A model that knows
#      what KVM is does not experience `KVM` as needing a definition.
#
# So: enumerate deterministically here, and hand the reviewer a list it
# cannot rationalize away — exactly how `structs_not_found` works.
#
# DESIGN. Two independent scans:
#   (a) curated terms — a hand-maintained set of FreeBSD/OS-internals terms
#       that need a gloss on first use. Curated on purpose: a generic
#       "hard word" heuristic would drown the writer in noise.
#   (b) unexpanded acronyms — any `[A-Z]{2,}` token that never appears
#       expanded anywhere in the chapter. Catches terms not on list (a),
#       which is the whole point: the curated list will always lag.
#
# Both scans only look at PROSE. Fenced code blocks are masked out: a term
# appearing in a struct definition or a DTrace script is not "use in prose"
# and demanding a gloss there would be wrong.
#
# WHAT THIS DELIBERATELY DOES NOT DO. It cannot tell a correct rationale
# from a plausible invented one — verifying a *why* claim is not
# symbol-shaped, so grep has no purchase on it. The `BIO_UNMAPPED`
# pageout-daemon confabulation above would still ship today. That is the
# same open gap as unverifiable ordering claims; see FUTURE_IMPROVEMENTS.md
# ("Rationale correctness is unverifiable"). This checker raises the floor
# (the term gets defined at all); it does not make the definition true.

# Terms that need a gloss on first prose use. Keep this list curated and
# ordered roughly by subsystem — it is meant to be read and edited by hand.
#
# Inclusion test: would a competent C programmer with no kernel experience
# have to go look this up? If yes, it belongs here. Terms a working C
# programmer already knows (pointer, struct, mutex, cache, syscall) must
# NOT be added: every entry costs writer attention, and a noisy list is
# how this check gets ignored.
#
# Multi-word entries are matched case-insensitively as phrases; short
# all-caps entries are matched case-sensitively as whole words so `KVM`
# does not fire on "kvm" inside an identifier.
_JARGON_TERMS: Tuple[str, ...] = (
    # Virtual memory / address space
    "KVM", "KVA", "pmap", "TLB shootdown", "superpage", "copy-on-write",
    "shadow chain", "page fault", "wired page", "resident set",
    "vm_object", "anonymous memory", "backing store",
    # Allocation
    # `zone` alone is dropped: it collides with UMA zones, jail zones and
    # DNS zones. `UMA zone` as a phrase is covered by `UMA` + `keg`.
    "UMA", "slab", "keg", "M_NOWAIT", "M_WAITOK", "malloc type",
    # Synchronization
    "turnstile", "witness", "epoch", "read-mostly lock", "lock order",
    "priority propagation", "memory barrier", "atomic",
    # I/O and storage
    "bio", "DMA", "MAXPHYS", "unmapped I/O", "strategy routine",
    "scatter-gather", "bounce buffer", "write-back", "write-through",
    # VFS
    # `vfs` alone is dropped: it matches the subsystem name in ordinary
    # prose ("the vfs layer") far more often than a term needing a gloss.
    "vnode", "namei", "mountpoint", "dirent", "inode",
    # Scheduling / process
    "kproc", "kthread", "thread0", "curthread", "context switch",
    "run queue", "preemption", "critical section",
    # Kernel infrastructure. `sysctl` is deliberately absent: it appears in
    # nearly every chapter, the reader meets it in chapter 1, and it is
    # already in `_ACRONYM_GLOSS_EXEMPT` — keeping it here fired ~1 finding
    # per chapter for a term nobody is confused by.
    "sbuf", "kld", "sysinit", "devclass", "newbus",
    "capability mode", "jail", "vnet",
    # Networking
    "mbuf", "pcb", "netisr", "if_snd",
)

# Acronyms that are common-knowledge enough not to need expansion, or that
# are FreeBSD-universal (a reader of this document set meets them in
# chapter 1). Without this set the acronym scan is unusably noisy.
_ACRONYM_GLOSS_EXEMPT: frozenset = frozenset({
    # General computing
    "CPU", "RAM", "ROM", "I/O", "IO", "OS", "API", "ABI", "FIFO", "LIFO",
    "LRU", "MRU", "ASCII", "UTF", "URL", "URI", "XML", "JSON", "YAML",
    "HTTP", "HTTPS", "TCP", "UDP", "IP", "IPV4", "IPV6", "MAC", "DNS",
    "SSH", "TLS", "SSL", "USB", "PCI", "PCIE", "SCSI", "SATA", "NVME",
    "GPU", "MMU", "TLB", "NUMA", "SMP", "ISA", "BIOS", "UEFI", "EFI",
    "GPT", "MBR", "DVD", "CD", "SSD", "HDD", "KB", "MB", "GB", "TB",
    "KIB", "MIB", "GIB", "TIB", "BSD", "GNU", "GPL", "AI", "ID", "OK",
    # FreeBSD-universal
    "VFS", "VM", "UFS", "ZFS", "NFS", "GEOM", "UMA", "KLD", "SYSCTL",
    "DTRACE", "SDT", "PID", "UID", "GID", "TTY", "FD", "ELF", "KVM",
    # Structural noise from markdown/code that survives masking
    "NOTE", "TODO", "FIXME", "WARNING", "IMPORTANT", "N", "C",
    "README", "SEE", "ALSO", "WHY", "HOW", "AND", "OR", "NOT", "ALL",
    # Direction / position abbreviations. Universally understood in a
    # driver context and expanding them ("transmit", "receive") adds
    # nothing a reader of a NIC chapter needs.
    "TX", "RX", "IN", "OUT", "UP", "DOWN", "SRC", "DST",
    # Protocol STATE names, not acronyms. These are literal identifiers
    # from the TCP state machine; "expanding" them is meaningless.
    "SYN", "ACK", "FIN", "RST", "LISTEN", "ESTABLISHED", "CLOSED",
    "CLOSING", "TIME", "WAIT", "LAST", "SENT", "RCVD",
    # Common network/hardware acronyms a reader of these chapters already
    # has. Kept narrow: only ones that appeared as noise in the 31-chapter
    # sweep, not a blanket networking dictionary.
    "ICMP", "ARP", "VLAN", "MTU", "MSS", "RTT", "RTO", "NIC", "PHY",
    "MII", "MSI", "MSIX", "IRQ", "APIC", "IOMMU", "MMIO", "MSR", "DMA",
    "FIB", "RSS", "TSO", "LRO", "GSO", "CRC", "TTL", "QOS",
    # SCSI/CAM command and field names appear in caps because that is how
    # the spec writes them; they are literals, not abbreviations.
    "READ", "WRITE", "INQUIRY", "CAPACITY", "SENSE", "CDB", "LUN",
    "REQUEST", "MODE", "START", "STOP", "TEST", "UNIT", "READY",
    # BSD queue/tree macros.
    "TAILQ", "STAILQ", "SLIST", "LIST", "RB", "SPLAY",
    # Architecture names.
    "ARM64", "AMD64", "I386", "RISCV", "POWERPC", "AARCH64",
})

# Model / revision tokens: `T4`, `T5`, `VT`, `SVM` and similar are chip or
# extension names, not acronyms with an expansion. A short token that is
# mostly digits, or a 2-letter token, carries no expandable meaning.
_ACRONYM_MODEL_RE = re.compile(r"^[A-Z]{1,2}[0-9]+$|^[A-Z]{2}$")

# A term is considered glossed if a definitional cue appears within this
# many characters after its first prose use. Tuned generously: the writer
# often defines a term in the sentence *following* first use, and a false
# "already glossed" is much cheaper than nagging about a defined term.
_GLOSS_WINDOW_CHARS = 400

# Phrases that mark an actual definition rather than mere use.
#
# Split into two tiers, because a single permissive pattern does not work.
# The first version of this check accepted a bare `(` or `:` anywhere in the
# 400-char window, and consequently found ch12's `KVM` "glossed" by an
# unrelated parenthetical 90 characters later — passing the synthetic tests
# while missing the exact document that motivated the checker. Punctuation
# is only a gloss when it is ADJACENT to the term.
#
# Tier 1: verbal cues, valid anywhere in the look-ahead window. These are
# unambiguous ("stands for", "is a") and the writer often puts them in the
# sentence after first use.
_GLOSS_CUE_RE = re.compile(
    r"(?:"
    r"\bis\s+(?:a|an|the)\b|\bare\s+(?:a|an|the)\b|"
    r"\bstands?\s+for\b|\brefers?\s+to\b|\bmeans?\b|"
    r"\bknown\s+as\b|\bi\.e\.|\bnamely\b|"
    r"\bin\s+other\s+words\b|\bshort\s+for\b"
    r")",
    re.IGNORECASE,
)

# Tier 2: punctuation gloss, only valid immediately after the term —
# "KVM (kernel virtual address space)", "KVM — the kernel's address
# space", "KVM: the kernel's address space". Anchored at position 0 of the
# post-term text, and the gloss body must contain at least two words so a
# bare cross-reference like "KVM (see below)" does not count.
_GLOSS_ADJACENT_RE = re.compile(
    r"^\s*(?:"
    r"\(\s*(?P<paren>[^)]{6,120})\)|"
    r"[—–]\s*(?P<dash>[^.;\n]{6,120})|"
    r":\s+(?P<colon>[^.;\n]{6,120})"
    r")",
)

# A tier-2 gloss body must look like an explanation, not a pointer.
_GLOSS_BODY_REJECT_RE = re.compile(
    r"^\s*(?:see|cf\.|section|chapter|below|above|figure|table|note)\b",
    re.IGNORECASE,
)


def _has_gloss(post_term_text: str) -> bool:
    """True if `post_term_text` opens with, or soon contains, a definition.

    `post_term_text` is the draft text immediately following a term's first
    prose use. Tier-2 punctuation cues are checked at offset 0 only; tier-1
    verbal cues are accepted anywhere in the window.
    """
    adj = _GLOSS_ADJACENT_RE.match(post_term_text)
    if adj:
        body = next(g for g in (adj.group("paren"), adj.group("dash"),
                                adj.group("colon")) if g is not None)
        if (len(body.split()) >= 2
                and not _GLOSS_BODY_REJECT_RE.match(body)):
            return True
    return bool(_GLOSS_CUE_RE.search(post_term_text))


def _mask_fenced_blocks(text: str) -> str:
    """Blank out fenced code blocks, preserving byte offsets.

    Offsets must survive because the jargon scan reports first-use
    positions and measures a look-ahead window against them. Replacing
    each block with same-length spaces keeps every later offset valid
    while making the block's content invisible to the scanners.
    """
    def _blank(m: "re.Match") -> str:
        # Keep newlines so line-oriented cues (and any future
        # line-number reporting) stay aligned.
        return "".join("\n" if c == "\n" else " " for c in m.group(0))

    return _FENCED_BLOCK_RE.sub(_blank, text)


def _strip_inline_code(text: str) -> str:
    """Blank out inline `code` spans, preserving offsets.

    Used only for the acronym scan. An acronym inside backticks is
    nearly always an identifier (`BIO_UNMAPPED`, `M_NOWAIT`) rather than
    prose the reader must parse, and the curated-term scan handles the
    ones that genuinely matter. Without this, every macro name in the
    chapter reads as an unexpanded acronym.
    """
    return re.sub(
        r"`[^`\n]*`",
        lambda m: " " * len(m.group(0)),
        text,
    )


def _find_glossary_section(text: str) -> str:
    """Return the body of a `## Glossary` section, or "".

    A term defined in an explicit Glossary counts as glossed no matter
    where else it appears, so the check must not nag about terms the
    chapter already lists there.
    """
    m = re.search(
        r"^##+\s*Glossary\s*$(.*?)(?=^##\s|\Z)",
        text, re.MULTILINE | re.DOTALL,
    )
    return m.group(1) if m else ""


def _term_pattern(term: str) -> "re.Pattern":
    """Build a whole-word matcher for a curated jargon term.

    Short all-caps terms (KVM, KVA, DMA, UMA) match case-sensitively so
    they don't fire on unrelated lowercase substrings; everything else
    matches case-insensitively because prose capitalisation varies
    ("Copy-on-write" at the start of a sentence).

    Hyphens and spaces in multi-word terms are treated interchangeably:
    the writer may render "copy-on-write" or "copy on write", and both
    are the same term.
    """
    flexible = re.escape(term).replace(r"\-", r"[-\s]").replace(r"\ ", r"[-\s]")
    if term.isupper() and len(term) <= 5:
        return re.compile(rf"\b{flexible}\b")
    return re.compile(rf"\b{flexible}\b", re.IGNORECASE)


def _extract_unglossed_jargon(text: str) -> List[str]:
    """Find curated jargon terms used in prose without a nearby gloss.

    Returns a sorted list of terms whose first prose use has no
    definitional cue within `_GLOSS_WINDOW_CHARS` and which are not
    defined in an explicit `## Glossary` section.
    """
    prose = _mask_fenced_blocks(text)
    glossary = _find_glossary_section(text)

    unglossed = []
    for term in _JARGON_TERMS:
        pat = _term_pattern(term)
        # Already defined in the Glossary section? Nothing to report.
        if glossary and pat.search(glossary):
            continue
        m = pat.search(prose)
        if not m:
            continue  # term not used in this chapter
        window = prose[m.end():m.end() + _GLOSS_WINDOW_CHARS]
        if not _has_gloss(window):
            unglossed.append(term)
    return sorted(set(unglossed))


# Matches a candidate acronym in prose: 2+ consecutive capitals, allowing
# embedded digits (IPv6 handled via the exempt set, PML4, AMD64).
_ACRONYM_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,})\b")


def _extract_unexpanded_acronyms(text: str) -> List[str]:
    """Find acronyms used in prose that are never expanded in the chapter.

    An acronym counts as expanded if the chapter contains a parenthetical
    or adjacent expansion whose initials match it — e.g. "kernel virtual
    memory (KVM)" or "KVM (kernel virtual memory)". This is the safety
    net for terms missing from `_JARGON_TERMS`; the curated list will
    always lag the source tree.

    Conservative by construction: inline code is masked, a broad exempt
    set is applied, and anything appearing fewer than twice is ignored
    (a one-off mention is rarely load-bearing).
    """
    prose = _strip_inline_code(_mask_fenced_blocks(text))

    counts: Dict[str, int] = {}
    for m in _ACRONYM_RE.finditer(prose):
        tok = m.group(1)
        counts[tok] = counts.get(tok, 0) + 1

    unexpanded = []
    for tok, n in counts.items():
        if n < 2 or tok.upper() in _ACRONYM_GLOSS_EXEMPT:
            continue
        if _ACRONYM_MODEL_RE.match(tok):
            continue  # chip/model/extension token, nothing to expand
        # Expansion adjacent to the acronym, either order.
        paren_after = re.search(
            rf"\b{re.escape(tok)}\b\s*\(([^)]{{4,80}})\)", prose)
        paren_before = re.search(
            rf"\(([^)]{{0,4}})?\s*{re.escape(tok)}\s*\)", prose)
        expanded = False
        if paren_after:
            initials = "".join(w[0] for w in re.findall(
                r"[A-Za-z]+", paren_after.group(1)))
            if tok.lower() in initials.lower() or len(
                    paren_after.group(1).split()) >= 2:
                expanded = True
        if paren_before:
            # "kernel virtual memory (KVM)" — words immediately before.
            head = prose[max(0, paren_before.start() - 80):paren_before.start()]
            if len(re.findall(r"[A-Za-z]+", head)) >= 2:
                expanded = True
        if not expanded:
            unexpanded.append(tok)
    return sorted(unexpanded)


# --- Struct-body field verification ---------------------------------------
#
# Writers paraphrase struct definitions from training-data memory: they
# describe `struct sysinit` as `{ int si_sub; int si_order; sysinit_func_t
# si_func; ... }` when the real definition in `sys/sys/kernel.h` is
# `{ enum sysinit_sub_id subsystem; enum sysinit_elem_order order;
# STAILQ_ENTRY(sysinit) next; sysinit_cfunc_t func; const void *udata; }`.
# Symbol-existence checks pass (`struct sysinit` exists), so the
# fabrication slips through. This pass parses each `struct NAME { ... }`
# code block in the draft, finds the real definition in the tree, and
# flags any claimed field name that doesn't appear in the real struct.
#
# Best-effort by design: if the real struct can't be parsed (nested
# enums, unusual macros), we return an empty real-field set and skip
# verification rather than risk false positives. Missing-struct cases
# are already caught by `_verify_structs`.

# Match a markdown fenced code block whose contents include a struct
# definition. We capture everything between the fences so we can scan
# multiple struct definitions in one block.
_FENCED_BLOCK_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)

# Within a code block, find one `struct NAME { ... };` definition.
# Use a non-greedy body match — code blocks may contain multiple structs,
# and nested braces are handled at the parse step (we strip nested
# `{...}` regions before tokenizing fields).
_STRUCT_DEF_RE = re.compile(
    r"struct\s+([a-zA-Z_]\w*)\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}",
    re.DOTALL,
)


def _strip_c_comments(text: str) -> str:
    """Strip /* ... */ and // ... comments from C source."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def _parse_struct_fields(body: str) -> List[str]:
    """Extract field names from a struct body.

    Best-effort: returns the last identifier on each `;`-terminated
    declarator. Strips comments, parenthesised macro arguments
    (`STAILQ_ENTRY(foo) next` → `STAILQ_ENTRY next` so `next` wins),
    bitfield widths, array shapes, and leading `*`s.

    Anonymous nested aggregates (`union { ... };` and `struct { ... };`
    with no trailing declarator) are recursed into so their inner
    field names surface — `struct mbuf` would otherwise contribute zero
    field names because every top-level chain pointer lives inside an
    anonymous union. *Named* nested aggregates (`enum { FOO } p_state`,
    `union { ... } u`) are still flattened to the trailing declarator.

    Empty list on parse trouble — callers must treat that as
    "verification unavailable," not as "no real fields."
    """
    text = _strip_c_comments(body)

    # Anonymous aggregate bodies: `union {...};` or `struct {...};` at a
    # declaration position with NO trailing declarator. C allows these
    # to contribute their inner field names directly to the enclosing
    # struct. Without recursion, the parser strips the `{...}` region
    # and emits the literal `union`/`struct` keyword as a field name —
    # which is also wrong but in a quieter way. Recurse first, then let
    # the rest of the parser handle named nested aggregates by stripping.
    fields: List[str] = []

    # Iteratively pull out `\b(union|struct)\s*\{...\}\s*;` blocks
    # (anonymous, immediately terminated). Recursion depth is bounded
    # by the brace-balancer's text-shrink invariant: each match removes
    # at least the keyword + braces.
    def _find_balanced_brace(s: str, open_idx: int) -> int:
        depth = 0
        for i in range(open_idx, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    return i
        return -1

    anon_re = re.compile(r"\b(?:union|struct)\s*\{")
    while True:
        m = anon_re.search(text)
        if not m:
            break
        open_idx = text.index("{", m.end() - 1)
        close_idx = _find_balanced_brace(text, open_idx)
        if close_idx < 0:
            break
        # Look at what follows the closing brace, skipping whitespace,
        # to decide anonymous vs named.
        after = text[close_idx + 1:].lstrip()
        if after.startswith(";"):
            # Anonymous: recurse, then remove the entire block from text.
            inner_body = text[open_idx + 1:close_idx]
            fields.extend(_parse_struct_fields(inner_body))
            # Compute the matching `;` offset to consume it too.
            semi_offset = text.find(";", close_idx + 1)
            text = text[:m.start()] + " " + (
                text[semi_offset + 1:] if semi_offset >= 0 else ""
            )
        else:
            # Named (`union {...} u;` etc.) — break out so the generic
            # `{...}` strip below collapses it to the trailing
            # declarator. We must advance past this match to avoid
            # re-finding it on the next loop iteration.
            text = (
                text[:m.start()] + " " + text[m.start() + len("union"):]
                if text[m.start():m.start() + len("union")] == "union"
                else text[:m.start()] + " " + text[m.start() + len("struct"):]
            )

    # Strip remaining nested {...} regions iteratively. An inline enum
    # or named anonymous struct body would otherwise corrupt the
    # declarator split (commas inside the inner body, terminating `;`
    # inside, etc.). Replace with a single space so the surrounding
    # identifier ("} p_state" → " p_state") survives.
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\{[^{}]*\}", " ", text)

    # Strip parenthesised macro arguments — `STAILQ_ENTRY(sysinit)`,
    # `LIST_HEAD(, proc)`, `TAILQ_HEAD(, thread)`. The macro identifier
    # itself is now adjacent to the field name; the field name is the
    # last token on the declarator, so the macro identifier won't win.
    text = re.sub(r"\([^()]*\)", " ", text)

    for decl in text.split(";"):
        decl = decl.strip()
        if not decl:
            continue
        # Bitfields: `int foo : 3` — keep only the side before `:`.
        decl = decl.split(":", 1)[0].strip()
        if not decl:
            continue
        # Drop array shapes: `char buf[BUFSIZ]` → `char buf`. The
        # leading identifier+`[` survives as `buf`.
        decl = re.sub(r"\[[^\]]*\]", "", decl)
        # Last whitespace-separated token, with leading `*` stripped.
        tokens = decl.split()
        if not tokens:
            continue
        last = tokens[-1].lstrip("*").rstrip(",")
        # Field names are C identifiers — guard against parse residue.
        # Reject the `union`/`struct` keywords which are emitted when
        # an anonymous block was recognised but couldn't be fully
        # parsed (defence-in-depth against the recursion path).
        if re.fullmatch(r"[A-Za-z_]\w*", last) and last not in {
            "union", "struct"
        }:
            fields.append(last)
    return fields


def _extract_struct_bodies(
    text: str,
) -> List[Tuple[str, List[str], str]]:
    """Find `struct NAME { ... }` claims in fenced code blocks.

    Returns a list of `(struct_name, [claimed_field_names], body_text)`
    tuples. Only claims inside fenced code blocks count — inline mentions
    of `struct sysinit` in prose are not field-list claims and shouldn't
    be flagged. The raw body text is returned alongside the parsed
    field list so the overlap-threshold check can detect "abridged"
    markers (`...`, `/* fields elided */`) which signal the writer
    deliberately shortened the struct.
    """
    claims: List[Tuple[str, List[str], str]] = []
    seen: set = set()
    for block in _FENCED_BLOCK_RE.finditer(text):
        for m in _STRUCT_DEF_RE.finditer(block.group(1)):
            name = m.group(1)
            body = m.group(2)
            # An empty body or a body whose only content is `...`
            # placeholder is the writer eliding fields, not claiming
            # any — skip.
            stripped = _strip_c_comments(body).strip().strip(".").strip()
            if not stripped:
                continue
            fields = _parse_struct_fields(body)
            if not fields:
                continue
            # Dedup on (name, sorted-field-set) so a struct shown twice
            # in the same draft is only reported once.
            key = (name, tuple(sorted(set(fields))))
            if key in seen:
                continue
            seen.add(key)
            claims.append((name, fields, body))
    return claims


# --- Function-signature claims --------------------------------------------
#
# The writer often emits real function names with the wrong arg count —
# the "verified hallucination" pattern: `daemon_init` is real, but the
# draft shows `int daemon_init(void)` while the source defines it as
# `int daemon_init(struct config *conf, bool debug)`. Names pass the
# existence grep, fields pass (no struct involved), but the signature
# is from 2022. We can't catch the prose-level behavior drift, but we
# can hard-fail an arity mismatch.
#
# Strictness is arity-only: comparing types runs into typedef
# equivalence (`u_int` vs `unsigned int` vs `uint32_t`) and produces
# false positives. Counting comma-separated top-level args is robust.
#
# Code fences only — call sites in prose (`daemon_init(conf)`) are not
# signature claims and produce arity noise on partial calls / variadic
# expansions / macro-style invocations. Same discipline as
# `_extract_struct_bodies`.

# Function-definition shape inside a code-fenced block. Mirrors the
# regex in `_extract_func_sigs` (line 1573) which works against real
# source files; the same shape matches inside markdown code fences
# because the writer-prompt rule says "copy verbatim into the code
# block." Two alternatives:
#   1. `RETTY *? NAME(args)` — single-line return type form.
#   2. `^NAME(args)` — K&R column-0 form with return type on previous
#      line. We accept it but cannot easily attribute a return type.
# `[^{;]*?` for the arg list lets it span lines (DOTALL).
_FUNC_SIG_RE = re.compile(
    r"(?:[A-Za-z_]\w*\s*\**\s+)+\*?\s*([A-Za-z_]\w*)\s*"
    r"\(([^{;]*?)\)\s*(?:__\w+(?:\s*\([^)]*\))?\s*)*\{",
    re.MULTILINE | re.DOTALL,
)

# Identifiers we never want to treat as a function definition even when
# the regex matches them — control-flow keywords look like `if (...) {`
# and would otherwise be flagged. Mirrors the filter in
# `_extract_func_sigs`.
_FUNC_SIG_KEYWORDS = frozenset({
    "if", "else", "while", "for", "switch", "return",
    "struct", "union", "enum", "typedef", "define",
    "do", "case", "default", "sizeof", "goto",
})


def _count_c_args(arglist: str) -> Optional[int]:
    """Count top-level args in a C parameter list. None if unparseable.

    Handles:
      - empty `()` and `(void)` → 0
      - top-level commas only — commas inside nested parens (function
        pointers like `int (*cmp)(void *, void *)`) are skipped
      - C comments (`/* ... */`, `//`) stripped first
      - trailing variadic `, ...` counts as one arg slot per the
        C standard's perspective on `va_arg` reading; conservative
        match keeps the explicit count and lets `...` add 1 — keeps
        the writer honest about claiming variadic-ness in the draft

    Returns None on K&R-style declarations (an arg list of bare
    identifiers with no type tokens) — those are too ambiguous to
    arity-check reliably and rare in modern FreeBSD code.
    """
    s = _strip_c_comments(arglist).strip()
    if not s:
        return 0
    # `(void)` is the C idiom for "explicitly no arguments." Tolerate
    # whitespace and a trailing `*` or `const` (writers sometimes write
    # `(const void)` paraphrasing — the intent is still zero args).
    if re.fullmatch(r"(?:const\s+)?void\s*\**", s):
        return 0
    # K&R smell test: arg list contains only identifiers + commas
    # (no `*`, no whitespace-separated type tokens). E.g. `(td, args)`.
    # A real prototype always has at least one type token alongside the
    # parameter name. If we see only bare identifiers, we cannot count
    # arity safely.
    if re.fullmatch(r"[A-Za-z_]\w*(\s*,\s*[A-Za-z_]\w*)*", s):
        return None

    depth = 0
    count = 1  # at least one argument once we've ruled out empty/void
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def _extract_function_signatures(
    text: str,
) -> List[Tuple[str, int, str]]:
    """Find function-definition signatures in fenced code blocks.

    Returns a list of `(func_name, arg_count, raw_signature_text)`
    tuples. Code fences only — see the module-level note for why
    inline backticked calls are excluded. Arity is the count from
    `_count_c_args`; entries where arity comes back None (K&R style,
    unparseable) are dropped — verification would be unreliable.

    Dedup on `(name, arity)` so the same definition shown twice in the
    same draft is reported once.
    """
    claims: List[Tuple[str, int, str]] = []
    seen: set = set()
    for block in _FENCED_BLOCK_RE.finditer(text):
        body = block.group(1)
        for m in _FUNC_SIG_RE.finditer(body):
            name = m.group(1)
            if name in _FUNC_SIG_KEYWORDS:
                continue
            arity = _count_c_args(m.group(2))
            if arity is None:
                continue
            key = (name, arity)
            if key in seen:
                continue
            seen.add(key)
            # Trim raw_sig to just the prototype line(s) up to the
            # opening brace — the writer doesn't need the function body
            # quoted back at them.
            raw = m.group(0)
            brace_idx = raw.rfind("{")
            if brace_idx > 0:
                raw = raw[:brace_idx].strip()
            claims.append((name, arity, raw))
    return claims


def _struct_body_is_abridged(body: str) -> bool:
    """True if the writer marked a struct body as deliberately shortened.

    Recognises the conventions a writer uses to say "I'm only showing
    the relevant fields":
      - bare `...` / `…` ellipsis on its own line
      - `/* ... */`, `/* … */`, `// ...` placeholder comments
      - explicit phrasing in any comment: `elided`, `omitted`,
        `truncated`, `simplified`, `abridged`, `abbreviated`,
        `for brevity`, `other fields`, `additional fields`,
        `more fields`

    The signal is intentionally permissive — false negatives on this
    check (treating a real abridged block as unmarked) lead to spurious
    flags, which is worse than letting through a few unmarked
    abridged blocks. The downstream check still requires zero overlap
    before flagging.
    """
    # Bare ellipsis on its own line, or `…` U+2026 (Unicode horizontal
    # ellipsis) anywhere. The Unicode form is a tell that the writer
    # paraphrased and let the model emit a "smart" ellipsis.
    if "\u2026" in body:
        return True
    if re.search(r"^\s*(?:\.\.\.|\.\.\.\s*;?)\s*$", body, re.MULTILINE):
        return True
    # Any comment containing the placeholder phrasing.
    for cm in re.finditer(r"/\*(.*?)\*/|//([^\n]*)", body, re.DOTALL):
        comment = (cm.group(1) or cm.group(2) or "").lower()
        if not comment:
            continue
        if re.search(r"\.\.\.|\u2026", comment):
            return True
        for marker in (
            "elided", "omitted", "truncated", "simplified", "abridged",
            "abbreviated", "for brevity", "other fields",
            "additional fields", "more fields", "rest of",
        ):
            if marker in comment:
                return True
    return False


# Cache the *real* field set per (src_root+search-roots, struct_name).
# Stored as frozenset; an empty frozenset means "looked but couldn't
# parse" and disables verification for that struct. The cache key
# encodes the widened search roots so a struct found under `stand/`
# for ch2 doesn't poison a `sys/`-only chapter that uses the same name.
_STRUCT_FIELDS_CACHE: Dict[Tuple[str, str], frozenset] = {}


def _real_struct_fields(struct_name: str, src_root: str,
                        extra_search_dirs: Optional[List[str]] = None
                        ) -> frozenset:
    """Locate `struct NAME { ... }` in the search tree and return its fields.

    Searches `<src_root>/sys` plus any directories in `extra_search_dirs`
    (relative paths joined to src_root, absolute paths used as-is, non-
    existent paths skipped). Empty frozenset on any of: definition not
    found, parse failure, or grep timeout. Callers must treat empty as
    "verification unavailable" rather than "no fields exist."
    """
    search_roots, cache_suffix = _resolve_search_roots(
        src_root, extra_search_dirs,
    )
    cache_key = (
        src_root + ("::" + cache_suffix if cache_suffix else ""),
        struct_name,
    )
    if cache_key in _STRUCT_FIELDS_CACHE:
        return _STRUCT_FIELDS_CACHE[cache_key]

    if not search_roots:
        _STRUCT_FIELDS_CACHE[cache_key] = frozenset()
        return frozenset()

    # Find files containing `struct NAME` — definition shape. We grep
    # only for the prefix (without the opening brace) because K&R-style
    # definitions write the brace on the next line (`struct foo\n{`),
    # which a fixed-string single-line grep can't match. The brace-aware
    # `_extract_struct_body` regex below handles both `struct NAME {`
    # and `struct NAME\n{`. The looser grep returns more candidates
    # (forward decls, function arguments) but `_extract_struct_body`
    # filters them out — only files that actually contain the
    # definition body parse non-None.
    pattern = f"struct {struct_name}"
    roots_arg = " ".join(f"{shlex.quote(r)}/" for r in search_roots)
    cmd = (
        f"grep -rlF --include='*.c' --include='*.h' "
        f"{shlex.quote(pattern)} {roots_arg} 2>/dev/null"
    )
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            errors="replace", timeout=_GREP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        # Verification unavailable this round. Return empty (callers treat
        # empty as "don't flag" — the fail-open contract is deliberate for
        # struct-body checks, where flagging every field on a transient
        # timeout would be a false-positive storm). But do NOT cache it:
        # a cached empty set would keep this struct unverifiable for the
        # rest of the run even after the pressure that caused the timeout
        # is gone.
        return frozenset()

    candidates = [
        line for line in result.stdout.splitlines() if line.strip()
    ]

    # Sort candidates by likelihood of being the canonical definition.
    # The `sys/sys/` and `sys/<arch>/include/` directories hold the
    # primary kernel headers; deeper paths (e.g. `sys/netpfil/ipfw/test/`)
    # often contain test stubs that mimic the real struct (a "fake mbuf"
    # with two fields). Sorting on path-depth-first underlies a `*.h`-
    # before-`*.c` preference; within `.h` files we prefer canonical
    # locations over deeper tree paths.
    def _candidate_priority(path: str) -> tuple:
        rel = os.path.relpath(path, src_root) if path.startswith(
            src_root) else path
        # Tier 0: canonical primary kernel header dirs.
        canonical = (
            "sys/sys/" in path
            or "/sys/sys/" in path
            or "include/" in path and ("/sys/" in path or path.startswith(
                "include/"))
        )
        is_h = path.endswith(".h")
        depth = rel.count(os.sep)
        return (
            0 if canonical and is_h else (1 if is_h else 2),
            depth,
            path,
        )

    candidates.sort(key=_candidate_priority)

    def _extract_struct_body(content: str, name: str) -> Optional[str]:
        """Brace-balanced body extraction. Returns None if not found."""
        # Match `struct NAME [\s\n]*{` then walk braces. Deep nesting
        # (mbuf has 3+ levels in its trailing union-of-unions) breaks
        # any pure-regex approach; use a hand-rolled balancer.
        for m in re.finditer(
            r"struct\s+" + re.escape(name) + r"\s*\{",
            content,
        ):
            open_idx = m.end() - 1
            depth = 0
            for i in range(open_idx, len(content)):
                c = content[i]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return content[open_idx + 1:i]
            # Unbalanced — try the next match.
        return None

    # Parse every reasonable candidate and pick the one with the most
    # fields. A test stub with 2 fields would otherwise short-circuit
    # the loop and rob the real definition of its richer field set —
    # which then defeats the overlap-threshold check below.
    # The grep pattern is `struct NAME` (no brace), so we get many
    # forward-decl/function-arg false candidates. Walk a generous slice
    # — `_extract_struct_body` returns None on files that don't actually
    # define the struct, so non-definition candidates cost only a file
    # read. The first definition we find under the canonical sort is
    # almost always right; we still pick max-fields across the slice to
    # tolerate test-stub headers in the noise.
    best_fields: List[str] = []
    best_path: Optional[str] = None
    for candidate in candidates[:32]:
        try:
            with open(candidate, "r", encoding="utf-8",
                      errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        body = _extract_struct_body(content, struct_name)
        if body is None:
            continue
        fields = _parse_struct_fields(body)
        if len(fields) > len(best_fields):
            best_fields = fields
            best_path = candidate

    if best_fields:
        real = frozenset(best_fields)
        _STRUCT_FIELDS_CACHE[cache_key] = real
        return real

    _STRUCT_FIELDS_CACHE[cache_key] = frozenset()
    return frozenset()


# Cache the *real* arity per (src_root+search-roots, func_name).
# Value tuple: `(arity, file_path)` — file_path lets us tell the
# writer where to read the real signature. `None` means "looked but
# couldn't parse" and disables verification for that name (so we
# don't false-flag K&R-style or attribute-heavy signatures we can't
# count). Same cache-key shape as `_STRUCT_FIELDS_CACHE` so widened
# search dirs (ch2's `["stand"]`) don't poison `sys/`-only chapters.
_FUNC_SIG_CACHE: Dict[Tuple[str, str], Optional[Tuple[int, str]]] = {}


def _real_function_signature(
    func_name: str,
    src_root: str,
    extra_search_dirs: Optional[List[str]] = None,
) -> Optional[Tuple[int, str]]:
    """Locate `func_name`'s definition in the search tree and return its arity.

    Searches `<src_root>/sys` plus any directories in `extra_search_dirs`.
    Returns `(arity, file_path)` on success or `None` on any of: not
    found, K&R-style declaration, parse failure, grep timeout. Callers
    must treat `None` as "verification unavailable" — never as "the
    function takes zero arguments."
    """
    search_roots, cache_suffix = _resolve_search_roots(
        src_root, extra_search_dirs,
    )
    cache_key = (
        src_root + ("::" + cache_suffix if cache_suffix else ""),
        func_name,
    )
    if cache_key in _FUNC_SIG_CACHE:
        return _FUNC_SIG_CACHE[cache_key]

    if not search_roots:
        _FUNC_SIG_CACHE[cache_key] = None
        return None

    # Find files containing the literal `func_name(` — definition or
    # call. K&R-style definitions have the name at column 0 followed by
    # `(`; modern definitions have it after a return type. Either way,
    # the candidate file must contain the literal opener.
    pattern = f"{func_name}("
    roots_arg = " ".join(f"{shlex.quote(r)}/" for r in search_roots)
    cmd = (
        f"grep -rlF --include='*.c' --include='*.h' "
        f"{shlex.quote(pattern)} {roots_arg} 2>/dev/null"
    )
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            errors="replace", timeout=_GREP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        # Verification unavailable this round — callers treat None as
        # "don't flag." Do NOT cache: a cached None would keep this
        # function unverifiable for the rest of the run even after the
        # transient timeout clears (see _real_struct_fields).
        return None

    candidates = [
        line for line in result.stdout.splitlines() if line.strip()
    ]

    # Prefer .c files (definitions live there) over .h (declarations
    # are usually prototypes — same arity, but parsing them risks
    # picking up `void foo(int);` forward decls vs. the real defn,
    # which is fine for arity but means we miss the body location for
    # the writer to re-read). Then by depth so canonical paths win
    # over deeply-nested test stubs.
    def _candidate_priority(path: str) -> tuple:
        rel = (
            os.path.relpath(path, src_root)
            if path.startswith(src_root) else path
        )
        is_c = path.endswith(".c")
        depth = rel.count(os.sep)
        # Test/stub paths last — same heuristic as struct lookup.
        is_test = "/test/" in path or "/tests/" in path
        return (
            0 if (is_c and not is_test) else (1 if is_c else 2),
            depth,
            path,
        )

    candidates.sort(key=_candidate_priority)

    # Walk candidates, run the function-definition regex, return the
    # first match whose name == target. The regex from `_extract_func_sigs`
    # only matches definitions (requires `{` after the arg list), so
    # forward declarations and call sites are rejected automatically.
    for candidate in candidates[:32]:
        try:
            with open(candidate, "r", encoding="utf-8",
                      errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        for m in _FUNC_SIG_RE.finditer(content):
            if m.group(1) != func_name:
                continue
            arity = _count_c_args(m.group(2))
            if arity is None:
                # Real source has K&R-style — we can't reliably count;
                # skip rather than false-flag the writer.
                continue
            rel_path = (
                os.path.relpath(candidate, src_root)
                if candidate.startswith(src_root) else candidate
            )
            result_tuple: Optional[Tuple[int, str]] = (arity, rel_path)
            _FUNC_SIG_CACHE[cache_key] = result_tuple
            return result_tuple

    _FUNC_SIG_CACHE[cache_key] = None
    return None


def _verify_struct_bodies(
    claims,
    src_root: str,
    extra_search_dirs: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """Verify struct-body claims against the real definition.

    `claims` is a list of `(name, claimed_fields, body_text)` from
    `_extract_struct_bodies`. (Older 2-tuple `(name, fields)` callers
    are still tolerated — the body check is then skipped for that
    entry.)

    Returns a pair of lists:

      - `bogus_fields_issues` — `"struct NAME: f1, f2"`, one per struct
        that has at least one claimed field which doesn't exist in the
        real struct. This is the original `_verify_struct_bodies`
        output.
      - `abridged_issues` — `"struct NAME (real has K fields, draft
        listed M with 0 overlap)"`, one per struct whose draft body
        has zero overlap with the real top-level fields AND the real
        struct has at least 4 fields AND the draft body is NOT marked
        abridged. Catches the original mbuf failure mode: a struct
        body that lists 1-of-30 real fields (or none) and looks
        plausible but isn't load-bearing.

    Structs whose real definition couldn't be located or parsed are
    silently skipped — `_verify_structs` already reports the "struct
    not in tree" case.
    """
    bogus_issues: List[str] = []
    abridged_issues: List[str] = []
    for entry in claims:
        if len(entry) == 3:
            name, claimed, body = entry
        else:
            name, claimed = entry
            body = ""
        real = _real_struct_fields(name, src_root, extra_search_dirs)
        if not real:
            continue  # verification unavailable — don't flag
        bogus = [f for f in dict.fromkeys(claimed) if f not in real]
        if bogus:
            # Include the REAL field list, not just the rejected names.
            # "these fields don't exist, go read the header" is not
            # actionable: on 2026-08-22 ch3 the writer looped for dozens of
            # steps insisting `si_sub`/`si_order`/`si_func` were right
            # ("the fact-checking says they don't exist. This is very
            # confusing.") because read_freebsd_source truncates the large
            # sys/sys/kernel.h before reaching the definition. We already
            # parsed the real fields to detect the mismatch — handing them
            # over turns a loop into a one-step edit. Sorted for stable
            # output; capped because a wide struct would otherwise crowd
            # out the other fact-check issues in the prompt.
            real_list = sorted(real)
            shown = ", ".join(real_list[:24])
            if len(real_list) > 24:
                shown += f", ... ({len(real_list)} fields total)"
            bogus_issues.append(
                f"struct {name}: {', '.join(bogus)} "
                f"(real fields are: {shown})"
            )

        # Overlap-threshold check. The original failure mode (mbuf,
        # 2026-05-01) was a draft body that named ~2 fields, none of
        # which existed in the real struct: the bogus-field check
        # reported them all, but a writer might equally invent 2
        # "fields" that happen to alias real names elsewhere. The
        # stronger signal is "the draft body has zero overlap with
        # the real top-level field set" — which means the writer
        # didn't read the source, just paraphrased from memory. Skip
        # small structs (≤3 fields) where 0-overlap is too easy to
        # hit by typo. Skip when the writer marked the body abridged.
        if body and len(real) >= 4 and not _struct_body_is_abridged(body):
            claimed_set = {f for f in claimed}
            overlap = claimed_set & real
            if not overlap:
                abridged_issues.append(
                    f"struct {name} (real definition has {len(real)} "
                    f"top-level fields, draft body lists "
                    f"{len(claimed_set)} with 0 overlap)"
                )
    return bogus_issues, abridged_issues


# Match `struct NAME [*]VAR;` declarations inside C code. Captures the
# struct name (group 1) and the variable name (group 2). The optional
# `*` is absorbed without capture; `[const|volatile]` qualifiers are
# tolerated between `struct` and the type. Trailing `=` (initialised
# decl) and `;` (plain decl) both terminate the match.
_STRUCT_VAR_DECL_RE = re.compile(
    r"\bstruct\s+([A-Za-z_]\w*)\s+\**\s*([A-Za-z_]\w*)\s*(?:[;,=)]|$)",
    re.MULTILINE,
)

# Match a member-access expression: VAR->FIELD or VAR.FIELD. The
# trailing lookahead rejects struct-name shadows (`foo.bar.baz` —
# we only want the immediate field); `\b` is enough.
_MEMBER_ACCESS_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*(?:->|\.)\s*([A-Za-z_]\w*)\b"
)


def _extract_struct_field_claims(
    text: str,
    known_structs: Optional[List[str]] = None,
) -> List[Tuple[str, str]]:
    """Find struct-field claims that aren't inside a `struct NAME {…}` block.

    Two evidence shapes, both load-bearing on the ch2 (Boot Process,
    2026-05-02) failure:

    1. **Member access in fenced C blocks** (`bi->bi_efi_memmap`):
       For each ```c``` block, scan for `struct NAME [*]VAR` declarations
       to build a `var → struct_name` map, then walk every `VAR->FIELD`
       and `VAR.FIELD` and emit `(struct_name, field)` for each VAR
       that resolved to a known struct.

    2. **Prose `STRUCTNAME->FIELD`** (`bootinfo->bi_efi_memmap`):
       Anywhere outside fenced code, a `NAME->FIELD` expression where
       `NAME` is the struct's *type* (not a variable) — this is the
       writer paraphrasing "the bootinfo's bi_efi_memmap field" into
       what looks like C syntax. We treat any `NAME` that appears in
       the chapter's `_extract_struct_names` set as a struct-name
       claim. False-positive risk is low: real C code doesn't use the
       struct-name as a variable name.

    Returns a deduplicated list of `(struct_name, field_name)` tuples.
    Fields and structs are NOT verified here — that's `_verify_struct_field_claims`.
    """
    claims: set = set()
    known = set(known_structs or [])

    # Stage 1: walk fenced ```c```-tagged blocks. Other fence languages
    # (mermaid, sh, ascii diagrams) don't have C semantics and can't
    # contribute reliable struct/field declarations.
    for block in _FENCED_BLOCK_RE.finditer(text):
        body = block.group(1)
        # Strip C comments first — variable names in `/* foo */` must
        # not promote to declarations.
        body = _strip_c_comments(body)
        # Build var→struct map from declarations in this block. Local
        # to the block: `struct foo *fp` declared in one snippet must
        # not bind in a different snippet.
        var_to_struct: Dict[str, str] = {}
        for m in _STRUCT_VAR_DECL_RE.finditer(body):
            struct_name, var = m.group(1), m.group(2)
            # Skip when the "var" is a C keyword (defensive against
            # parse residue); _STRUCT_VAR_DECL_RE's terminator set
            # already filters most of these.
            if var in {"const", "volatile", "static", "inline",
                       "register", "auto", "extern"}:
                continue
            var_to_struct[var] = struct_name
        # Walk member-access expressions and bind to struct.
        for m in _MEMBER_ACCESS_RE.finditer(body):
            var, field = m.group(1), m.group(2)
            if var in var_to_struct:
                claims.add((var_to_struct[var], field))

    # Stage 2: prose `STRUCTNAME->FIELD` outside fenced code. The
    # writer paraphrases "the bootinfo's bi_efi_memmap field" as
    # `bootinfo->bi_efi_memmap` even though no variable named
    # `bootinfo` exists — a tell that the field claim was written
    # from memory.
    if known:
        # Strip fenced blocks so member-access in code (handled by
        # Stage 1) doesn't double-trigger.
        prose = _FENCED_BLOCK_RE.sub("", text)
        for m in _MEMBER_ACCESS_RE.finditer(prose):
            var, field = m.group(1), m.group(2)
            if var not in known:
                continue
            # File-extension collision: `bootinfo.c`, `bootinfo.h`,
            # `bootinfo.S` etc. look syntactically like `var.field`
            # but are actually file paths. The writer routinely back-
            # ticks paths (`stand/efi/loader/bootinfo.c`) and the path
            # token gets caught by `_MEMBER_ACCESS_RE`. Reject any
            # field that matches a common source-file extension.
            if field in _FILE_EXT_DENYLIST:
                continue
            claims.add((var, field))

    return sorted(claims)


# Source/asset file extensions that appear as the suffix of a backticked
# path like `stand/efi/loader/bootinfo.c` and are misread as a struct
# member access by `_MEMBER_ACCESS_RE`. Listed here rather than tested
# against `os.path.splitext` because the prose form may not be a real
# file path (`bootinfo.c` mid-sentence) — we just need the lexical
# signal that it's a path-like construct.
_FILE_EXT_DENYLIST = frozenset({
    "c", "h", "S", "s", "py", "sh", "md", "yaml", "yml", "txt", "conf",
    "tex", "in", "am", "mk", "asm", "cc", "cpp", "hpp", "go", "rs",
    "log", "out", "err", "json", "xml", "html", "rst",
})


def _verify_struct_field_claims(
    claims: List[Tuple[str, str]],
    src_root: str,
    extra_search_dirs: Optional[List[str]] = None,
) -> List[str]:
    """For each `(struct, field)` claim, flag fields not in the real struct.

    Returns `"struct NAME->field"` strings, one per bogus claim.
    Skips structs whose real definition can't be located or parsed —
    `_verify_structs` already reports the "struct not in tree" case,
    and an unparseable struct would silently swallow every field
    claim if we treated empty-real-set as "no fields exist."
    """
    issues = []
    for struct_name, field in claims:
        real = _real_struct_fields(struct_name, src_root, extra_search_dirs)
        if not real:
            continue  # verification unavailable — don't flag
        if field not in real:
            issues.append(f"struct {struct_name}->{field}")
    return issues


def _verify_function_signatures(
    claims: List[Tuple[str, int, str]],
    src_root: str,
    extra_search_dirs: Optional[List[str]] = None,
    funcs_missing_set: Optional[set] = None,
) -> List[str]:
    """For each `(func_name, claimed_arity, raw_sig)` claim, flag arity mismatches.

    Returns issue strings shaped:

        "daemon_init: draft shows 0 args, real signature in
         sys/kern/kern_daemon.c has 2 args"

    Skips:
      - functions in `funcs_missing_set` (already reported by
        `_verify_functions`; double-reporting just adds noise)
      - functions whose real signature can't be located or parsed
        (returns None from `_real_function_signature` — silent skip)
      - K&R-style draft signatures (filtered out at extraction time)
    """
    issues = []
    missing = funcs_missing_set or set()
    seen: set = set()
    for name, claimed_arity, _raw in claims:
        if name in missing:
            continue
        # Same name+arity reported once even across multiple chapters
        # in the unlikely batch case; key is local to this call.
        if (name, claimed_arity) in seen:
            continue
        seen.add((name, claimed_arity))
        real = _real_function_signature(name, src_root, extra_search_dirs)
        if real is None:
            continue  # verification unavailable — don't flag
        real_arity, real_path = real
        if claimed_arity != real_arity:
            issues.append(
                f"{name}: draft shows {claimed_arity} arg"
                f"{'' if claimed_arity == 1 else 's'}, "
                f"real signature in {real_path} has "
                f"{real_arity} arg{'' if real_arity == 1 else 's'}"
            )
    return issues


def fact_check_draft(draft: str, src_root: str,
                     extra_search_dirs: Optional[List[str]] = None) -> dict:
    """Run structured fact-checking on a draft chapter.

    `extra_search_dirs` widens struct/function verification beyond
    `<src_root>/sys`. Each entry is a directory relative to `src_root`
    (or absolute). Used by chapters whose subject is genuinely outside
    `sys/` — e.g. ch2 (Boot Process) declares `["stand"]` so symbols
    like `EFI_MEMORY_DESCRIPTOR`, `preloaded_file`, and `elf64_exec`
    that live under `stand/` verify cleanly. Without it, the fact-fix
    loop tells the writer to "remove or correct" symbols that are
    actually correct.

    Returns a dict with:
        - 'file_paths_not_found': list of missing paths
        - 'file_paths_corrected': list of "wrong → right" pairs
        - 'structs_not_found': list of missing struct names
        - 'struct_fields_bogus': list of "struct N: f1, f2" strings
          (claims inside `struct NAME { ... }` code blocks)
        - 'struct_bodies_abridged': list of "struct N (real has K, draft
          listed M with 0 overlap)" strings — a fenced `struct NAME {…}`
          block whose field set has zero overlap with the real
          definition's top-level fields, AND the body is not marked
          abridged. This is the original mbuf failure mode.
        - 'struct_field_refs_bogus': list of "struct N->f" strings
          (member-access claims like `bi->bi_efi_memmap` outside a
          `struct NAME { ... }` block)
        - 'funcs_not_found': list of missing function names
        - 'func_sigs_mismatch': list of "name: draft shows N args, real
          signature in PATH has M args" strings — the function name is
          real but the draft's fenced ```c definition has the wrong
          arity. Catches the "verified hallucination" pattern where the
          writer pulled a 2022-era signature from training data.
        - 'kernel_options_not_found': list of unverifiable kernel-config options
        - 'dtrace_probes_not_found': list of unverifiable DTrace SDT probes
        - 'malloc_tags_not_found': list of unverifiable MALLOC_DEFINE tags
        - 'sysctls_not_found': list of sysctl OID paths not found as resolved
          nodes in the codebase-memory-mcp graph. Sysctl OIDs are the one
          symbol class grep cannot verify (the dotted path is assembled from
          SYSCTL_* macros and appears nowhere as a literal). ALWAYS empty
          when the graph backend is unavailable — this verifier degrades to
          a no-op rather than blocking the pipeline. Reported as "suspect,
          verify against sysctl(8)" not "hallucinated", because ~1/3 of OID
          nodes have an unresolved parent chain (false-negative risk).
        - 'jargon_unglossed': list of curated jargon terms (see
          `_JARGON_TERMS`) used in prose with no definitional cue within
          `_GLOSS_WINDOW_CHARS` of first use and not defined in a
          `## Glossary` section. A READABILITY finding, not an accuracy
          one — the term is real, it just isn't explained. Motivated by
          ch12 using `KVM` five times without ever defining it.
        - 'acronyms_unexpanded': list of acronyms used 2+ times in prose
          that are never expanded anywhere in the chapter. Safety net for
          terms missing from the curated list.
        - 'total_issues': count of all issues. EXCLUDES the two readability
          keys above by design — see the comment at the return statement.

    Any legacy `## Comparison` section is stripped before extraction
    (the section was removed from the pipeline in 2026-05; new drafts
    no longer produce it, but on-disk content from earlier runs still
    contains Linux/macOS/NetBSD symbol names that must not be grepped
    against the FreeBSD source tree).
    """
    fact_text = _strip_comparison_section(draft)
    file_paths = _extract_file_paths(fact_text)
    structs = _extract_struct_names(fact_text)
    struct_body_claims = _extract_struct_bodies(fact_text)
    # Field-ref claims need the struct-name set to recognize prose
    # `STRUCTNAME->FIELD` paraphrases (the writer using the type name
    # as if it were a variable). Run after `_extract_struct_names`.
    struct_field_ref_claims = _extract_struct_field_claims(
        fact_text, known_structs=structs,
    )
    funcs = _extract_function_names(fact_text)
    sig_claims = _extract_function_signatures(fact_text)
    kernel_options = _extract_kernel_options(fact_text)
    dtrace_probes = _extract_dtrace_probes(fact_text)
    malloc_tags = _extract_malloc_tags(fact_text)
    sysctls = _extract_sysctls(fact_text)
    # Readability, not existence: terms used in prose with no definition
    # nearby. Unlike every other check here this one needs no source tree,
    # so it runs unconditionally and never fails open. See the section
    # comment above `_JARGON_TERMS` for the ch12 `KVM` post-mortem.
    unglossed_jargon = _extract_unglossed_jargon(fact_text)
    unexpanded_acronyms = _extract_unexpanded_acronyms(fact_text)

    paths_missing = _verify_file_paths(file_paths, src_root)
    paths_corrected = [x for x in paths_missing if ' → ' in x]
    paths_missing = [x for x in paths_missing if ' → ' not in x]

    structs_missing = _verify_structs(structs, src_root, extra_search_dirs)
    # Only verify field bodies for structs whose name *does* exist in
    # the tree — `_verify_structs` already flags unknown struct names,
    # and trying to parse a real definition for a non-existent struct
    # would always come back empty (silent skip) anyway.
    structs_missing_set = set(structs_missing)
    body_claims_filtered = [
        (name, fields, body) for name, fields, body in struct_body_claims
        if name not in structs_missing_set
    ]
    struct_fields_bogus, struct_bodies_abridged = _verify_struct_bodies(
        body_claims_filtered, src_root, extra_search_dirs,
    )
    # Same filter for ref-claims: if `struct foo` is itself missing,
    # don't double-report every field access against it.
    field_ref_claims_filtered = [
        (name, field) for name, field in struct_field_ref_claims
        if name not in structs_missing_set
    ]
    struct_field_refs_bogus = _verify_struct_field_claims(
        field_ref_claims_filtered, src_root, extra_search_dirs,
    )
    funcs_missing = _verify_functions(funcs, src_root, extra_search_dirs)
    # Arity-mismatch check runs after `_verify_functions` so we can
    # skip names already flagged as missing — double-reporting just
    # adds noise to the fact-fix prompt.
    sig_mismatches = _verify_function_signatures(
        sig_claims, src_root, extra_search_dirs,
        funcs_missing_set=set(funcs_missing),
    )
    kernel_options_missing = _verify_kernel_options(kernel_options, src_root)
    dtrace_probes_missing = _verify_dtrace_probes(dtrace_probes, src_root)
    malloc_tags_missing = _verify_malloc_tags(malloc_tags, src_root)
    # Sysctl OIDs are verified against the codebase-memory-mcp graph, not
    # grep (the dotted path is never a literal in the source). No-ops to an
    # empty list when the graph backend is unavailable — see
    # `_verify_sysctls_via_graph` and CODE_MAP.md "Optional sysctl fact-check".
    sysctls_missing = _verify_sysctls_via_graph(sysctls)

    return {
        'file_paths_not_found': paths_missing,
        'file_paths_corrected': paths_corrected,
        'structs_not_found': structs_missing,
        'struct_fields_bogus': struct_fields_bogus,
        'struct_bodies_abridged': struct_bodies_abridged,
        'struct_field_refs_bogus': struct_field_refs_bogus,
        'funcs_not_found': funcs_missing,
        'func_sigs_mismatch': sig_mismatches,
        'kernel_options_not_found': kernel_options_missing,
        'dtrace_probes_not_found': dtrace_probes_missing,
        'malloc_tags_not_found': malloc_tags_missing,
        'sysctls_not_found': sysctls_missing,
        # Readability findings. Deliberately NOT in `total_issues`: that
        # count gates the fact-fix loop, whose prompt is about removing
        # hallucinated symbols. An undefined term is not a false claim, and
        # forcing a fact-fix round for one would (a) change when that loop
        # runs on nearly every chapter and (b) mix "delete this lie" with
        # "explain this term" in one instruction — the exact prompt-blending
        # that FUTURE_IMPROVEMENTS.md records as having backfired before.
        # These are surfaced to the REVIEWER instead, which already owns
        # accessibility (criterion 5) and rationale (criterion 8).
        'jargon_unglossed': unglossed_jargon,
        'acronyms_unexpanded': unexpanded_acronyms,
        'total_issues': (len(paths_missing) + len(paths_corrected) +
                         len(structs_missing) + len(struct_fields_bogus) +
                         len(struct_bodies_abridged) +
                         len(struct_field_refs_bogus) +
                         len(funcs_missing) +
                         len(sig_mismatches) +
                         len(kernel_options_missing) +
                         len(dtrace_probes_missing) +
                         len(malloc_tags_missing) +
                         len(sysctls_missing)),
    }


def _build_fact_check_prompt(chapter: dict, draft: str, facts: dict) -> str:
    """Build a prompt for the writer to fix fact-checking issues."""
    issues = []
    if facts['file_paths_not_found']:
        issues.append(
            f"File paths that do not exist in the source tree: "
            f"{', '.join(facts['file_paths_not_found'])}. "
            f"Remove or correct these references."
        )
    if facts['file_paths_corrected']:
        corrections = '; '.join(
            f"`{old}` should be `{right}`"
            for old, right in (x.split(' → ') for x in facts['file_paths_corrected'])
        )
        issues.append(f"Corrected paths: {corrections}.")
    if facts['structs_not_found']:
        issues.append(
            f"Structs not found in source tree: "
            f"{', '.join(facts['structs_not_found'])}. "
            f"Remove or correct these with real definitions from header files."
        )
    if facts.get('struct_fields_bogus'):
        # Each entry is `"struct NAME: f1, f2"` — the named fields do
        # not exist in the real struct. Tell the writer to read the
        # defining header verbatim rather than paraphrase from memory;
        # the original `struct sysinit` post-mortem (see
        # FUTURE_IMPROVEMENTS.md) is the canonical example of why this
        # check exists.
        issues.append(
            "Struct field names that do not exist in the real struct: "
            + "; ".join(facts['struct_fields_bogus'])
            + ". The struct itself is real but the fields you listed are "
            "not — they are almost always stale names from an older "
            "version of the tree. The real field list is given in "
            "parentheses above: use exactly those names. Do NOT re-derive "
            "them by reading the header; that list came from the current "
            "source and is authoritative. You may elide fields with "
            "`/* ... */` but must not rename or retype the ones you keep."
        )
    if facts.get('struct_bodies_abridged'):
        # Each entry is `"struct NAME (real definition has N top-level
        # fields, draft body lists M with 0 overlap)"` — the struct
        # itself exists, but the fenced ```c struct NAME { ... } ``` in
        # the draft does not include any of its real top-level fields.
        # That's the signature of paraphrasing the body from memory —
        # the original `struct mbuf` failure mode (see
        # FUTURE_IMPROVEMENTS.md, "Abridged-structs / overlap threshold"
        # post-mortem). The body might have all-fabricated names that
        # individually pass the bogus-field check (because they're
        # plausible) but collectively bear no relation to the real
        # struct.
        issues.append(
            "Struct bodies whose field set has zero overlap with the "
            "real source definition: "
            + "; ".join(facts['struct_bodies_abridged'])
            + ". The struct exists, but the fenced ```c struct NAME "
            "{ ... } ``` block you wrote does not include any of its "
            "real top-level fields — that's the signature of "
            "paraphrasing from memory. Open the defining header with "
            "`read_freebsd_source` (or "
            "`resolve_c_definition(symbol=\"struct NAME\")`) and quote "
            "the real top-level field names verbatim. You may elide "
            "fields with `/* ... */` (or write `/* fields elided */` "
            "in a comment), but the named fields you keep MUST be ones "
            "that exist in the real definition. Do NOT invent "
            "plausible-sounding field names or rename real ones."
        )
    if facts.get('struct_field_refs_bogus'):
        # Each entry is `"struct NAME->field"` — a member-access
        # expression (in a code block) or a paraphrased
        # `STRUCTNAME->FIELD` (in prose) that names a field which
        # doesn't exist in the real struct. ch2 (Boot Process,
        # 2026-05-02) shipped with `bi->bi_efi_memmap`,
        # `bi->bi_efi_memmap_size`, `bootinfo->bi_modlist` even though
        # `struct bootinfo` (sys/i386/include/bootinfo.h:48) has none
        # of those fields. The fix is the same as for `struct_fields_bogus`:
        # read the defining header before naming a field.
        issues.append(
            "Struct field references that name a non-existent field: "
            + "; ".join(facts['struct_field_refs_bogus'])
            + ". The struct exists but the field does not. Open the "
            "defining header with `read_freebsd_source` (or call "
            "`resolve_c_definition(symbol=\"struct NAME\")`) and verify "
            "the real field name before referencing it. Either correct "
            "the field name to a real one, or remove the sentence/line "
            "that uses the fabricated field. Do NOT invent plausible-"
            "sounding field names."
        )
    if facts['funcs_not_found']:
        issues.append(
            f"Functions not found in source tree: "
            f"{', '.join(facts['funcs_not_found'])}. "
            f"Remove or correct these with real function signatures."
        )
    if facts.get('func_sigs_mismatch'):
        # Each entry: "name: draft shows N args, real signature in PATH
        # has M args". The function is real (it survived `_verify_functions`)
        # but the writer wrote the wrong arity in a fenced ```c block —
        # the canonical "verified hallucination" tell where the model
        # pulled a 2022-era signature from training data while the real
        # source has since changed. Arity mismatch strongly suggests the
        # behavior description in the surrounding prose is also stale,
        # so the prompt explicitly asks the writer to re-read the body
        # — not just patch the signature line.
        issues.append(
            "Function signatures in your code blocks do not match the "
            "real function definitions: "
            + "; ".join(facts['func_sigs_mismatch'])
            + ". The function name is real but the argument count you "
            "wrote is wrong. This is the signature of paraphrasing the "
            "function from memory rather than reading the current "
            "source. Open the file with `read_freebsd_source` (or call "
            "`resolve_c_definition(symbol=\"<name>\")`) and copy the "
            "real signature verbatim. IMPORTANT: an arity mismatch "
            "almost always means the function's behavior has changed "
            "too — re-read the body and revise any prose that "
            "describes what the function does, do not just patch the "
            "signature line in isolation."
        )
    if facts.get('kernel_options_not_found'):
        issues.append(
            f"Kernel-config options not found in sys/conf/options* or "
            f"sys/conf/NOTES: "
            f"{', '.join(facts['kernel_options_not_found'])}. "
            f"These appear hallucinated — remove the claim or verify "
            f"the option name against `sys/conf/NOTES`."
        )
    if facts.get('dtrace_probes_not_found'):
        issues.append(
            f"DTrace SDT probes not found in any SDT_PROBE_DEFINE* macro: "
            f"{', '.join(facts['dtrace_probes_not_found'])}. "
            f"These appear hallucinated — remove the claim or replace "
            f"with a real probe (grep for `SDT_PROBE_DEFINE` in sys/)."
        )
    if facts.get('malloc_tags_not_found'):
        issues.append(
            f"MALLOC_DEFINE/MALLOC_DECLARE tags not found in sys/: "
            f"{', '.join(facts['malloc_tags_not_found'])}. "
            f"These appear hallucinated — remove the claim or replace "
            f"with a real tag (grep for `MALLOC_DEFINE` in sys/)."
        )
    if facts.get('sysctls_not_found'):
        # Each entry is a sysctl OID path (`net.inet.tcp.foo`) that has no
        # matching resolved `Sysctl` node in the codebase-memory-mcp graph.
        # Deliberately softer wording than the other checks: the graph
        # cannot resolve ~1/3 of real OIDs (macro-arg / runtime-built parent
        # chains), so a "not found" here is SUSPECT, not proven fabricated.
        # We ask the writer to confirm against the authoritative runtime
        # source (`sysctl(8)`) or the registering SYSCTL_* macro, and to
        # remove the claim if they cannot — never to trust the flag blindly.
        issues.append(
            "Sysctl OID paths that could not be verified against the source "
            "tree's registered sysctls: "
            + ", ".join(f"`{s}`" for s in facts['sysctls_not_found'])
            + ". These are SUSPECT (possibly hallucinated, possibly just "
            "unverifiable). A sysctl OID is real only if some SYSCTL_* macro "
            "registers it. Confirm each against the real registration — grep "
            "`sys/` for the leaf name inside a `SYSCTL_` macro, e.g. the last "
            "segment of `net.inet.tcp.foo` would appear as `SYSCTL_...(..., "
            "OID_AUTO, foo, ...)` — or against `sysctl -a` output if you can "
            "cite it. If you cannot confirm the OID exists, remove the "
            "sentence that names it. Do NOT invent tunable names."
        )

    return textwrap.dedent(f"""\
        You are revising an existing chapter for "FreeBSD Internals."
        A fact-checking pass found specific symbol/path issues — fix
        ONLY those, leave the rest of the draft untouched.

        ## Chapter: {chapter['title']}

        ## Fact-Checking Issues

        {chr(10).join(f"- {iss}" for iss in issues)}

        ## Current Draft

        {draft}

        ## Your Task — PATCH MODE, NOT REWRITE MODE

        Treat this like a code-review patch: minimal, targeted edits.

        1. Fix EVERY issue listed above — but ONLY those issues. Do not
           rewrite paragraphs that weren't flagged.
        2. For corrected paths, just substitute the correct path in place.
        3. For a missing struct/function, replace it with a verified real
           one OR remove just that reference. Prefer replacement.
        4. Do NOT re-explore the source tree broadly. Only call
           `read_freebsd_source` / `resolve_c_definition` for the SPECIFIC
           symbols flagged above — one targeted lookup per symbol, not a
           tree walk.
        5. Keep everything else unchanged. Copy unflagged paragraphs
           through verbatim.

        ## Step Budget

        You have a hard step limit. Each tool call costs a step, and if
        you run out before emitting the corrected chapter via
        `final_answer(...)`, your work is discarded and the unfixed draft
        is kept (with hallucinations still in it). Keep lookups
        surgical.

        ## Output

        Output ONLY the complete corrected Markdown via `final_answer(...)`.
        No preamble, no explanation of changes, no diff. Emit the FULL
        chapter — every section, including parts you didn't change.
    """).lstrip()


# ---------------------------------------------------------------------------
# 4c. Cross-README Navigation Links
# ---------------------------------------------------------------------------


def _build_chapter_rels(chapters: List[dict]) -> dict:
    """Build the title -> [related-titles] map used for sidebars and See-Also.

    Source of truth is `chapters.yaml`:
      - `related:` (per chapter) is taken verbatim if set;
      - otherwise we fall back to "all other chapters with the same `family:`"
        (a chapter never lists itself).

    This replaced a hand-maintained `CHAPTER_RELS` dict that duplicated every
    chapter title from chapters.yaml. The duplication is the bug to avoid: a
    rename in chapters.yaml leaves the dict pointing at stale strings, and the
    failure mode is silent — `dict.get(title, [])` returns [], the sidebar
    renders empty, and the See-Also injection becomes a no-op.

    Titles in `related:` MUST match a `title:` in chapters.yaml exactly.
    Unknown titles are filtered out (warned to stderr) so a typo doesn't
    crash navigation but is visible in the run log.
    """
    titles = {ch["title"] for ch in chapters}
    by_family: dict = {}
    for ch in chapters:
        fam = ch.get("family")
        if fam:
            by_family.setdefault(fam, []).append(ch["title"])

    rels: dict = {}
    for ch in chapters:
        title = ch["title"]
        explicit = ch.get("related")
        if explicit is not None:
            cleaned = []
            for r in explicit:
                if r in titles and r != title:
                    cleaned.append(r)
                else:
                    print(
                        f"  [warn] chapter '{title}' has unknown related "
                        f"title '{r}' — ignored",
                        file=sys.stderr,
                    )
            rels[title] = cleaned
        else:
            fam = ch.get("family")
            rels[title] = [t for t in by_family.get(fam, []) if t != title]
    return rels


_AUTO_GEN_BANNER = (
    "<!-- This file is auto-generated by generate-doc.py "
    "-- do not edit manually -->"
)


def _strip_existing_nav_block(content: str) -> str:
    """Remove the auto-generated nav block (banner + sidebar) if present.

    Each insertion of the nav sidebar adds:
        <auto-gen banner>
        <blank>
        ---
        **Navigation:**
        ...lines...
        ---
        <blank>

    We scan for the banner line, then drop everything from the banner
    up to (and including) the closing `---` of the sidebar, plus any
    immediately following blank line. Any banner not followed by a
    sidebar block is also removed (covers the case where an early run
    wrote only the banner). Repeated runs of `--nav-only` previously
    accumulated multiple of these blocks; this strip is what makes the
    rebuild idempotent.
    """
    lines = content.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == _AUTO_GEN_BANNER.strip():
            # Skip banner line.
            i += 1
            # Optional blank.
            if i < len(lines) and lines[i].strip() == "":
                i += 1
            # If a `---` opens a Navigation block, skip through its
            # closing `---` too.
            if (
                i < len(lines)
                and lines[i].strip() == "---"
                and i + 1 < len(lines)
                and lines[i + 1].strip().startswith("**Navigation:**")
            ):
                i += 1  # opening ---
                while i < len(lines) and lines[i].strip() != "---":
                    i += 1
                if i < len(lines):
                    i += 1  # closing ---
                if i < len(lines) and lines[i].strip() == "":
                    i += 1  # trailing blank
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _build_ancestor_chain(current_file: str,
                          all_files: "set[str]") -> "list[str]":
    """Return the chapter files that are ancestors of `current_file`.

    Closest first, root last. Two kinds of ancestor:

    1. The plain `README.md` in the *same directory* as a non-plain
       README (e.g. `sys/vm/README.md` is an ancestor of
       `sys/vm/README_bcache.md`). The leaf README treats the
       directory's main README as the canonical "go up one level"
       target.
    2. Any chapter whose `output_file` lives in a strict-prefix
       directory of the current file's directory. The closest such
       chapter is preferred over deeper ones.

    Same-directory siblings that are *not* the plain README.md are
    NOT ancestors — those are peers (e.g. all the `sys/kern/README_*`
    files are peers of each other, not parents).
    """
    cur_dir = os.path.dirname(current_file)
    cur_base = os.path.basename(current_file)
    out: list[str] = []
    # 1. Same-dir README.md sibling (if I'm a README_<topic>).
    if cur_base != "README.md":
        candidate = os.path.join(cur_dir, "README.md") if cur_dir else "README.md"
        if candidate in all_files and candidate != current_file:
            out.append(candidate)
    # 2. Strict-prefix-directory ancestors. For each ancestor dir
    # (closest first), pick whichever chapter file lives there.
    if cur_dir:
        parts = cur_dir.split("/")
        for i in range(len(parts) - 1, -1, -1):
            anc_dir = "/".join(parts[:i])  # "" for root
            for f in all_files:
                if f == current_file or f in out:
                    continue
                if os.path.dirname(f) == anc_dir:
                    out.append(f)
    return out


def build_navigation(chapters: List[dict]) -> dict:
    """Build navigation links across all READMEs and a master index.

    Returns a dict mapping output_file -> updated markdown content.
    Also writes a master README at SRC_ROOT/README.all-chapters.md.
    """
    # Build a title -> output_file lookup
    title_map = {}
    rel_map = {}
    for ch in chapters:
        title = ch["title"]
        output_file = ch.get("output_file", "README.md")
        title_map[title] = output_file
        rel_map[output_file] = ch

    # Derive the title -> related-titles map from chapters.yaml
    # (`related:` per chapter, or family-mates as fallback). Single
    # source of truth — see _build_chapter_rels.
    chapter_rels = _build_chapter_rels(chapters)

    updated = {}

    # Pre-pass: collect every term defined in any chapter's Glossary, so
    # the first-use linker can point a term at whichever chapter actually
    # defines it. Must run before the rewrite loop — a chapter processed
    # early otherwise cannot link to a definition in a chapter processed
    # later.
    #
    # First definition wins on collision. Chapters are processed in
    # chapters.yaml order, so the winner is stable across runs rather
    # than depending on directory-walk order; two chapters defining the
    # same term is itself a content smell worth seeing in the count.
    glossary_index: Dict[str, Tuple[str, str]] = {}
    for ch in chapters:
        gf = ch.get("output_file", "README.md")
        gp = os.path.join(SRC_ROOT, gf)
        if not os.path.exists(gp):
            continue
        try:
            with open(gp, errors="replace") as f:
                gtext = f.read()
        except OSError:
            continue
        for gterm, ganchor in _parse_glossary_entries(gtext).items():
            glossary_index.setdefault(gterm, (gf, ganchor))
    if glossary_index:
        print(
            f"  [links] glossary index: {len(glossary_index)} term(s) "
            f"across all chapters"
        )

    for ch in chapters:
        output_file = ch.get("output_file", "README.md")
        output_path = os.path.join(SRC_ROOT, output_file)
        if not os.path.exists(output_path):
            continue

        with open(output_path) as f:
            content = f.read()

        title = ch["title"]

        # Build navigation sidebar
        nav_links = []
        all_chapters = []
        related = chapter_rels.get(title, [])

        # Pre-compute lookups for the ancestor chain: file -> title.
        file_to_title = {
            other.get("output_file", "README.md"): other["title"]
            for other in chapters
        }
        all_files: set[str] = set(file_to_title.keys())

        for other in chapters:
            other_title = other["title"]
            other_file = other.get("output_file", "README.md")
            if other_file == output_file:
                continue
            # Calculate the relative link from the current README's
            # directory to the other README. The earlier hand-rolled
            # string math produced wrong paths like "../../..sys/vm/..."
            # for cross-directory pairs (missing a slash, wrong number of
            # ../ segments). os.path.relpath does this correctly for any
            # source/target pair, including when either side is at the
            # tree root.
            cur_abs = os.path.join(SRC_ROOT, output_file)
            other_abs = os.path.join(SRC_ROOT, other_file)
            rel_path = os.path.relpath(other_abs, start=os.path.dirname(cur_abs))
            rel_link = f"[{other_title}]({rel_path})"
            all_chapters.append(rel_link)
            if other_title in related:
                nav_links.append(rel_link)

        # Build the ancestor breadcrumb (closest -> root) so a reader
        # can walk up one directory at a time. See _build_ancestor_chain
        # for the rule (same-dir README.md sibling + strict-prefix
        # directory chapters). Same os.path.relpath logic as the cross-
        # links above.
        ancestor_files = _build_ancestor_chain(output_file, all_files)
        ancestor_links = []
        cur_dir_abs = os.path.dirname(os.path.join(SRC_ROOT, output_file))
        for anc_file in ancestor_files:
            anc_abs = os.path.join(SRC_ROOT, anc_file)
            anc_rel = os.path.relpath(anc_abs, start=cur_dir_abs)
            ancestor_links.append(f"[{file_to_title[anc_file]}]({anc_rel})")

        # Build the sidebar
        sidebar_lines = [
            _AUTO_GEN_BANNER,
            "",
            "---",
            "**Navigation:**",
        ]
        if ancestor_links:
            sidebar_lines.append(f"  **Up:** {' ▸ '.join(ancestor_links)}")
        if nav_links:
            sidebar_lines.append(f"  **Related:** {' | '.join(nav_links[:5])}")
        sidebar_lines.append(f"  **All chapters:** {' | '.join(all_chapters[:8])}{' ...'}")
        sidebar_lines.append("---")
        sidebar_lines.append("")

        # Strip any pre-existing auto-generated nav block before
        # re-inserting the fresh one. Without this, every nav rebuild
        # *prepends* a new block while leaving the old one in place,
        # so READMEs accumulate duplicate banners and stale links each
        # time --nav-only or post-processing runs.
        content = _strip_existing_nav_block(content)

        # Insert sidebar after the title (first line starting with #)
        lines = content.split("\n")
        insert_idx = 0
        for i, line in enumerate(lines):
            if line.startswith("# "):
                insert_idx = i + 1
                break

        lines.insert(insert_idx, "\n".join(sidebar_lines))
        content = "\n".join(lines)

        # Update "See Also" section with cross-links
        content = _add_see_also_links(
            content, title, title_map, output_file, chapter_rels
        )

        # Repair broken cross-chapter `.md` links. Catches legacy stale
        # paths baked into existing READMEs (the post-mortem in
        # FUTURE_IMPROVEMENTS.md "See Also block: wrong relative-path
        # depth" only fixed *fresh* link insertion; existing files kept
        # the wrong paths) AND any future writer drift. Rewrites when a
        # target's basename uniquely identifies a chapter, drops the
        # list-item otherwise. Idempotent.
        content, _ln_rewrote, _ln_dropped = _sanitize_chapter_links(
            content, output_file, set(title_map.values()),
        )
        if _ln_rewrote or _ln_dropped:
            print(
                f"  [links] {output_file}: rewrote {_ln_rewrote}, "
                f"dropped {_ln_dropped} broken .md link(s)"
            )

        # Drop docs.freebsd.org handbook/articles links whose slug isn't
        # in $FREEBSD_DOC. The writer hallucinates plausible-looking
        # slugs (e.g. `/handbook/geom-class/` instead of `/handbook/geom/`)
        # that no other fact-check layer notices because the URL never
        # points into the source tree. Validating against the on-disk
        # doc tree is deterministic and free.
        content, _doc_dropped = _sanitize_doc_urls(content, output_file)
        if _doc_dropped:
            print(
                f"  [links] {output_file}: dropped {_doc_dropped} "
                f"unknown docs.freebsd.org link(s)"
            )

        # Wrap bare backtick source-file paths in See Also as relative
        # markdown links so the rendered README is clickable in a browser.
        content, _src_linked = _link_see_also_source_paths(
            content, output_file,
        )
        if _src_linked:
            print(
                f"  [links] {output_file}: linked {_src_linked} "
                f"See Also source path(s)"
            )

        # Wrap inline `name(N)` man-page references as relative links
        # to the source-tree mdoc file (e.g. `src.conf(5)` →
        # `share/man/man5/src.conf.5`). Whole body, not just See Also,
        # because chapter prose mentions man pages constantly.
        content, _man_linked = _link_manpage_refs(content, output_file)
        if _man_linked:
            print(
                f"  [links] {output_file}: linked {_man_linked} "
                f"man-page reference(s)"
            )

        # Link the first prose use of each glossary term to its
        # definition — in-chapter (`#anchor`) when this chapter defines
        # it, cross-chapter otherwise. Runs last of the four linkers so
        # it sees, and skips, every link the earlier three inserted.
        content, _gloss_linked = _link_glossary_first_use(
            content, output_file, glossary_index,
        )
        if _gloss_linked:
            print(
                f"  [links] {output_file}: linked {_gloss_linked} "
                f"glossary first-use(s)"
            )

        updated[output_file] = content

    # Write updated files
    written = 0
    for output_file, content in updated.items():
        output_path = os.path.join(SRC_ROOT, output_file)
        _atomic_write(output_path, content)
        written += 1

    # Write master README
    master_path = os.path.join(SRC_ROOT, "README.all-chapters.md")
    master_lines = [
        "# FreeBSD Internals — Chapter Index",
        "",
        "AI-generated documentation of FreeBSD internals. Each chapter is placed",
        "in the relevant FreeBSD source directory so any reader can find educational",
        "material right next to the code.",
        "",
        "## Chapters",
        "",
    ]
    for i, ch in enumerate(chapters, 1):
        title = ch["title"]
        output_file = ch.get("output_file", "README.md")
        output_path = os.path.join(SRC_ROOT, output_file)
        if os.path.exists(output_path):
            master_lines.append(f"{i}. [{title}]({output_file})")
        else:
            master_lines.append(f"{i}. [{title}] — not yet generated")
    master_lines.append("")
    master_lines.append("---")
    master_lines.append(f"*Generated by generate-doc.py — {len(chapters)} chapters*")

    _atomic_write(master_path, "\n".join(master_lines))

    print(f"  [navigation] updated {written} READMEs, wrote {master_path}")
    return updated


def _add_see_also_links(content: str, title: str, title_map: dict,
                        current_file: str, chapter_rels: dict) -> str:
    """Add cross-links to the See Also section of a README.

    Idempotent: any list-item line in the See Also section whose link
    target points at a known chapter README (i.e. matches a value in
    `title_map`) is treated as auto-generated and stripped before fresh
    links are inserted. The writer's own See Also content uses different
    relative paths (e.g. `vm/README.md` from inside `sys/`) and is left
    alone. Without this, every nav rebuild prepended another copy and
    READMEs accumulated dozens of duplicate link blocks
    (sys/vm/README_bcache.md hit 14 copies on 2026-04-30).
    """
    related = chapter_rels.get(title, [])

    # Find the See Also section
    see_also_idx = content.find("\n## See Also\n")
    if see_also_idx == -1:
        see_also_idx = content.find("\n## See Also")
    if see_also_idx == -1:
        return content

    # ---- Strip any prior auto-generated list items in this section ----
    # We treat a list-item as auto-generated if its link target resolves
    # (when joined with this file's directory and normalized) to another
    # chapter's README. This catches both correctly-built links from the
    # current code AND legacy broken links from earlier versions that
    # over-counted depth (e.g. `../../../sys/vm/README_bcache.md` from a
    # file in `sys/kern/` — wrong path, but still recognizably aimed at
    # a chapter README).
    current_dir = os.path.dirname(current_file) or "."
    chapter_files: set[str] = {
        ch_file for ch_file in title_map.values() if ch_file != current_file
    }
    chapter_basenames: set[str] = {os.path.basename(f) for f in chapter_files}

    # Section spans from after the header line to the next `## ` heading
    # or end of file. Operate line-by-line within that span.
    header_end = see_also_idx + len("\n## See Also")
    # Skip to end of header line (handles "## See Also" with trailing
    # whitespace or text on the same line).
    nl_after_header = content.find("\n", header_end)
    if nl_after_header == -1:
        return content
    body_start = nl_after_header + 1

    # Find end of section (next top-level heading or EOF).
    next_h2 = re.search(r"(?m)^## ", content[body_start:])
    body_end = body_start + next_h2.start() if next_h2 else len(content)

    section_body = content[body_start:body_end]
    auto_link_re = re.compile(r"^\s*-\s*\[[^\]]+\]\(([^)]+)\)")
    kept_lines = []
    for line in section_body.split("\n"):
        m = auto_link_re.match(line)
        if m:
            target = m.group(1).strip()
            # Treat as auto-inserted if (a) basename matches a chapter
            # README filename AND (b) the target's normalized form points
            # at one of the known chapter files. Resolving `current_dir +
            # target` and normalizing collapses both correct and
            # over-deep relative paths to the same canonical form.
            tgt_base = os.path.basename(target)
            if tgt_base in chapter_basenames:
                joined = os.path.normpath(os.path.join(current_dir, target))
                # Strip any leading "../" left over from over-deep paths
                # that climb above the repo root.
                while joined.startswith("../"):
                    joined = joined[3:]
                if joined in chapter_files:
                    continue  # drop a prior auto-inserted link (any depth)
        kept_lines.append(line)
    # Collapse runs of >1 blank lines that the strip may have left behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines))
    content = content[:body_start] + cleaned + content[body_end:]

    if not related:
        return content

    # ---- Build fresh cross-links ----
    # Path is computed relative to the directory containing `current_file`,
    # so e.g. sys/kern/README_locking.md → sys/vm/README_bcache.md
    # becomes "../vm/README_bcache.md", not "../../../sys/vm/...".
    links = []
    for rel_title in related:
        rel_file = title_map.get(rel_title)
        if rel_file and rel_file != current_file:
            rel_path = os.path.relpath(rel_file, start=current_dir)
            links.append(f"[{rel_title}]({rel_path})")

    if not links:
        return content

    # Insert links after the See Also header. The find() above looked for
    # "\n## See Also" (note the leading newline), so the matched span is
    # 12 chars long, not 11. Using len("## See Also") here would land the
    # insertion *inside* the heading — splitting "Also" into "Als" + "o"
    # in the rendered README.
    insert_pos = see_also_idx + len("\n## See Also")
    link_text = "\n" + "\n".join(f"- {l}" for l in links) + "\n"
    return content[:insert_pos] + link_text + content[insert_pos:]


# ---------------------------------------------------------------------------
# 4d. Cross-Chapter Reference Index
# ---------------------------------------------------------------------------


## Headers that mark the *start* of the overview section we want to lift
## out for the master index. Listed by preference — the first one found
## wins. Match is case-insensitive and tolerates either `#` or `##` so a
## chapter that drifts on capitalisation or heading level still parses.
_OVERVIEW_HEADERS = ("Quick Summary", "Overview")

## Headers that mark the *end* of the overview section. Anything between
## the start header and the first of these is the summary. Same case
## tolerance as above. Derived from _SECTION_CATALOG so adding a new
## section there automatically extends this set — except `Quick Summary`,
## which is the *start* header, not an end.
_OVERVIEW_END_HEADERS = tuple(
    name for name in _SECTION_CATALOG if name != "Quick Summary"
)


def _find_md_header(content: str, name: str, start_at: int = 0) -> int:
    """Return the offset of the next markdown header `name` (case-insensitive,
    `#` or `##`), or -1 if not found. The match is anchored at start of line
    so it doesn't fire inside a code block or paragraph that mentions the
    phrase.
    """
    # `[ ]{0,3}` — literal spaces only. Plain `\s` matches newlines, which
    # combined with `^` in multiline mode would slide the match to the
    # newline *before* the header, throwing off the header-line bounds.
    pattern = r'(?im)^[ ]{0,3}#{1,2}[ \t]+' + re.escape(name) + r'[ \t]*$'
    m = re.search(pattern, content[start_at:])
    if m is None:
        return -1
    return start_at + m.start()


def _extract_overview(content: str, max_chars: int = 300) -> str:
    """Extract the Quick Summary section from a README as a summary."""
    # Find the first overview-style header (case-insensitive, # or ##).
    start = -1
    matched_header_end = 0
    for name in _OVERVIEW_HEADERS:
        pos = _find_md_header(content, name)
        if pos != -1:
            start = pos
            # Find the end of this header line so the body extraction
            # doesn't include the header text itself.
            nl = content.find('\n', pos)
            matched_header_end = nl + 1 if nl != -1 else len(content)
            break
    if start == -1:
        return ""

    # Find the next section header that bounds the overview.
    end = len(content)
    for name in _OVERVIEW_END_HEADERS:
        pos = _find_md_header(content, name, start_at=matched_header_end)
        if pos != -1 and pos < end:
            end = pos

    overview = content[matched_header_end:end].strip()
    # Take first 2 paragraphs.
    paragraphs = re.split(r'\n\s*\n', overview)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    summary = "\n\n".join(paragraphs[:2])
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(' ', 1)[0] + "..."
    return summary


def _extract_glossary_terms(content: str) -> List[str]:
    """Extract key FreeBSD-specific terms from a README."""
    terms = set()
    # C structs: "struct foo"
    for m in re.finditer(r'\bstruct\s+([a-zA-Z_]\w*)\b', content):
        name = m.group(1)
        if name not in ('struct', 'structs', 'structname'):
            terms.add(name)
    # FreeBSD-specific identifiers (common kernel terms)
    known_terms = [
        'vm_map', 'vm_page', 'vnode', 'pmap', 'proc', 'thread', 'sched',
        'buf', 'bufobj', 'filedesc', 'kerneldesc', 'sysent', 'ucred',
        'label', 'mount', 'namecache', 'bufcache', 'uma_zone', 'vm_domain',
        'vm_object', 'vm_pageq', 'vm_zone', 'vmmeter', 'pcb', 'td', 'mdcpu',
        'cpu_info', 'scheddom', 'runq', 'pri', 'tdq', 'pcb', 'mdglobal',
        'intr_event', 'intr_handle', 'callout', 'sx', 'mtx', 'rwlock',
        'lockmgr', 'sbuf', 'uma', 'zone', 'taskqueue', 'task', 'workqueue',
        'vnet', 'domainset', 'blist', 'bqueue', 'bioq', 'bufqueue',
        'fifofs', 'sockbuf', 'sockbuf', 'socket', 'domain', 'protosw',
        'ifnet', 'ifqueue', 'ifaddr', 'rtentry', 'rtsock', 'rt_metrics',
        'in_ifaddr', 'in_multi', 'mbuf', 'pkthdr', 'sk_buff',
    ]
    for term in known_terms:
        if re.search(r'\b' + re.escape(term) + r'\b', content):
            terms.add(term)
    return sorted(terms)


def build_chapter_index(chapters: List[dict], src_root: str,
                        output_dir: str) -> str:
    """Build CHAPTER_INDEX.md with TOC, cross-references, and glossary.

    output_dir — where to write (typically SCRIPT_DIR, the project root)
    """
    print("  reading generated READMEs for index content ...")

    # Collect per-chapter data
    chapter_data = []
    all_glossary_terms = set()
    term_chapters = {}  # term -> list of chapter titles

    for ch in chapters:
        title = ch["title"]
        output_file = ch.get("output_file", "README.md")
        output_path = os.path.join(src_root, output_file)

        if not os.path.exists(output_path):
            chapter_data.append({
                'title': title,
                'output_file': output_file,
                'overview': None,
                'glossary': [],
            })
            continue

        with open(output_path) as f:
            content = f.read()

        overview = _extract_overview(content)
        glossary = _extract_glossary_terms(content)
        chapter_data.append({
            'title': title,
            'output_file': output_file,
            'overview': overview,
            'glossary': glossary,
        })
        for term in glossary:
            all_glossary_terms.add(term)
            if term not in term_chapters:
                term_chapters[term] = []
            term_chapters[term].append(title)

    # Build the index document
    lines = [
        "# FreeBSD Internals — Chapter Index",
        "",
        "AI-generated documentation of FreeBSD internals. Each chapter is placed",
        "in the relevant FreeBSD source directory so any reader can find educational",
        "material right next to the code.",
        "",
        "---",
        "",
        "## Table of Contents",
        "",
    ]

    for i, cd in enumerate(chapter_data, 1):
        title = cd['title']
        output_file = cd['output_file']
        overview = cd['overview']

        if overview:
            lines.append(f"{i}. **[{title}]({output_file})**")
            lines.append(f"   {overview}")
        else:
            lines.append(f"{i}. [{title}]({output_file}) — not yet generated")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Cross-References")
    lines.append("")

    # Group chapters by relationship (derived from chapters.yaml)
    chapter_rels = _build_chapter_rels(chapters)
    for title, related in chapter_rels.items():
        if not related:
            continue
        rel_links = []
        for rel_title in related:
            # Find the output file for this related chapter
            for cd in chapter_data:
                if cd['title'] == rel_title:
                    rel_links.append(f"[{rel_title}]({cd['output_file']})")
                    break
            else:
                rel_links.append(rel_title)
        lines.append(f"- **{title}** → {', '.join(rel_links)}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Glossary")
    lines.append("")
    lines.append("Key FreeBSD-specific terms found across all chapters, with which")
    lines.append("chapters discuss them:")
    lines.append("")

    # Sort glossary terms alphabetically
    for term in sorted(all_glossary_terms):
        chapters_list = term_chapters.get(term, [])
        if chapters_list:
            ch_links = []
            for ct in chapters_list:
                for cd in chapter_data:
                    if cd['title'] == ct:
                        ch_links.append(f"[{ct}]({cd['output_file']})")
                        break
                else:
                    ch_links.append(ct)
            lines.append(f"- **{term}**: {', '.join(ch_links)}")
        else:
            lines.append(f"- **{term}**")
    lines.append("")
    lines.append(f"*{len(all_glossary_terms)} terms across {len(chapter_data)} chapters*")

    # Write the index
    index_path = os.path.join(output_dir, "CHAPTER_INDEX.md")
    _atomic_write(index_path, "\n".join(lines))

    print(f"  [index] wrote {index_path} ({len(all_glossary_terms)} terms, "
          f"{len(chapter_data)} chapters)")
    return index_path


def main():
    parser = argparse.ArgumentParser(
        description="FreeBSD Internals — Documentation Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--chapter", type=int, default=0,
        help="Run only this chapter (1-based). 0 = all chapters.",
    )
    parser.add_argument(
        "--index-only", action="store_true",
        help="Build book index and exit (don't run agent).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate even if README.md already exists.",
    )
    parser.add_argument(
        "--reindex", action="store_true",
        help="Rebuild book index from scratch (ignore cached hashes).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without running agents.",
    )
    parser.add_argument(
        "--max-revisions", type=int, default=3,
        help="Max review+revise rounds per chapter (0 = no review, default=3).",
    )
    parser.add_argument(
        "--nav-only", action="store_true",
        help="Rebuild cross-README navigation links only (no agent runs).",
    )
    parser.add_argument(
        "--index", action="store_true",
        help="Rebuild CHAPTER_INDEX.md (TOC, glossary, cross-refs) only.",
    )
    args = parser.parse_args()

    # Advisory stack-dumper for silent hangs. Costs one sleeping thread;
    # turns "the chapter stopped and nobody knows why" into a stack trace
    # in the log. Disable with DAEMONDOCS_HANG_DUMP_SEC=0.
    install_hang_detector()

    # Validate environment
    if not os.path.isdir(SRC_ROOT):
        print(f"Error: FreeBSD source not found at {SRC_ROOT}")
        print(f"       Set FREEBSD_SRC to point to your tree.")
        sys.exit(1)

    if not os.path.isdir(BOOKS_DIR):
        print(f"Error: Books directory not found at {BOOKS_DIR}")
        print(f"       Set BOOKS_DIR to point to your books.")
        sys.exit(1)

    print("=" * 60)
    print("  FreeBSD Internals — Doc Generator")
    print("=" * 60)
    print(f"  Source:  {SRC_ROOT}")
    print(f"  Books:   {BOOKS_DIR}")
    print(f"  Index:   {INDEX_DIR}")
    print()

    # Phase 1: Extract books and build index
    print("[Phase 1] Book corpus extraction")
    corpus_path = build_book_corpus(BOOKS_DIR, force=args.reindex)

    print("[Phase 1b] FreeBSD source documentation")
    extract_freebsd_docs(SRC_ROOT, corpus_path)
    print()

    print("[Phase 2] TF-IDF index")
    index = get_or_build_index(corpus_path, force=args.reindex)

    if args.index_only:
        print("Done (index-only mode).")
        return

    if args.nav_only:
        chapters = load_chapters()
        print(f"\n[Phase 4] Cross-README navigation links")
        build_navigation(chapters)
        print("Done (nav-only mode).")
        return

    if args.index:
        chapters = load_chapters()
        print(f"\n[Phase 5] Cross-chapter reference index")
        build_chapter_index(chapters, SRC_ROOT, str(SCRIPT_DIR))
        print("Done (index-only mode).")
        return

    # Phase 3: Run agent per chapter
    all_chapters = load_chapters()
    chapters = all_chapters
    if args.chapter:
        if 1 <= args.chapter <= len(all_chapters):
            chapters = [all_chapters[args.chapter - 1]]
        else:
            print(f"Error: --chapter must be 1..{len(all_chapters)}")
            sys.exit(1)

    # Filter out already-done chapters (unless --force)
    to_run = []
    for ch in chapters:
        out = os.path.join(SRC_ROOT, ch.get("output_file", "README.md"))
        if not args.force and os.path.exists(out):
            print(f"  skip  {ch['title']} (README.md exists, use --force)")
        else:
            to_run.append(ch)

    if not to_run and not args.dry_run:
        print("\nAll chapters done. Use --force to regenerate.")
        return
    elif not to_run and args.dry_run:
        print("\nAll chapters would be skipped (all README.md exist). Use --force.")
        return

    print(f"\n[Phase 3] {len(to_run)} chapter(s) to process")

    if args.dry_run:
        for ch in to_run:
            run_chapter(ch, None, None, 0, dry_run=True)
        print(f"\n{'=' * 60}")
        print(f"  Dry-run complete")
        print(f"{'=' * 60}")
        return

    writer = create_writer_agent(index)
    reviewer = create_reviewer_agent(index)
    ok = 0
    for ch in to_run:
        if run_chapter(ch, writer, reviewer, args.max_revisions):
            ok += 1

    # Post-processing: cross-README navigation links.
    # Always pass the *full* chapter list, even when --chapter N filtered
    # `chapters` down to one. Otherwise build_navigation iterates over the
    # one filtered chapter and produces an empty "All chapters: ..." sidebar
    # (the for-other-in-chapters loop sees nothing but self and skips it).
    if ok > 0:
        print(f"\n[Phase 4] Cross-README navigation links")
        build_navigation(all_chapters)

        # Post-processing: cross-chapter reference index
        print(f"\n[Phase 5] Cross-chapter reference index")
        build_chapter_index(all_chapters, SRC_ROOT, str(SCRIPT_DIR))

    print(f"\n{'=' * 60}")
    print(f"  Done: {ok}/{len(to_run)} chapters generated")
    print(f"{'=' * 60}")

    # Exit non-zero when nothing was produced. A misconfiguration that makes
    # every LLM call fail (e.g. an unsupported kwarg rejected by the openai
    # SDK before it reaches the wire) otherwise still exits 0, so a queue
    # runner reads it as success and burns the rest of the queue at ~60s per
    # chapter with no output. 2026-08-22 lost 4 chapters that way.
    if ok == 0 and to_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
