#!/usr/bin/env python3
"""
FreeBSD Internals — Documentation Generator

Uses smolagents to produce README.md files throughout the FreeBSD
source tree. Each chapter is driven by chapters.yaml and the agent has
access to:
  - The FreeBSD source code  (read_freebsd_source)
  - A semantic search index of FreeBSD books (search_books)
  - The source tree structure (explore_tree)

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
import glob
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import textwrap
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
}

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
    _atomic_write(str(corpus_file), "".join(new_corpus_parts))

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
            capture_output=True, text=True, timeout=30,
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
        "sys/net/socket.c",
        "sys/net/netisr.c",
        "sys/net/if.c",
        "sys/netinet/ip_input.c",
        "sys/ufs/ffs/ffs_softdep.c",
        "sys/kern/subr_bus.c",
        "sys/kern/kern_intr.c",
        "sys/vm/buf2.c",
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
        kept = [segments[0]] if segments else []  # leading text before first source
        freebsd_prefixes = ("FreeBSD man9:", "FreeBSD paper:", "FreeBSD Handbook:",
                            "FreeBSD git log:", "FreeBSD kerneldoc:", "FreeBSD sys/")
        for i in range(1, len(segments), 2):
            src = segments[i].strip()
            if not any(src.startswith(p) for p in freebsd_prefixes):
                kept.append(segments[i])
                if i + 1 < len(segments):
                    kept.append(segments[i + 1])
        old_content = "".join(kept) if kept else ""

        with open(corpus_file, "w", encoding="utf-8") as f:
            f.write(old_content)
            for label, text in all_entries:
                f.write(f"\n\n### SOURCE: {label} ###\n\n")
                f.write(text)
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
        full = os.path.join(SRC_ROOT, path)
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
        full = os.path.join(SRC_ROOT, path)
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
    """Extract function signatures from C source code."""
    sigs = []
    # Match: type func_name(args) { or static type func_name(args) {
    for m in re.finditer(
        r'(?:static\s+|inline\s+|extern\s+)*'
        r'[\w\s\*]+\s+(\w+)\s*\([^)]*\)\s*\{',
        content
    ):
        name = m.group(1)
        # Skip common non-function keywords
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

        if start_file and os.path.exists(os.path.join(SRC_ROOT, start_file)):
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
                    full_path = os.path.join(SRC_ROOT, current)
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

                    # Also search for #define macros
                    for m in re.finditer(
                        rf'#define\s+{re.escape(symbol)}\s+(.+)',
                        content
                    ):
                        macro_body = m.group(1).strip()[:200]
                        found_defs.append(f"#define {symbol} {macro_body} (from {fpath})")

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
    "Comparison": {
        "template_body": (
            "(How other OSes implement the same concept. Focus on Linux: note\n"
            "key structural differences — e.g., FreeBSD's vm_map vs Linux's\n"
            "vm_area_struct, UMA vs SLUB, sx locks vs rw_semaphore. Also mention\n"
            "macOS/XNU, NetBSD, or OpenBSD where relevant. Keep it brief — 2-4\n"
            "paragraphs. Do not fabricate Linux file paths or line numbers.)"
        ),
        "rubric_body": "Linux/macOS/NetBSD structural differences",
    },
    "See Also": {
        "template_body": "(related chapters and source directories to explore next)",
        "rubric_body": "related chapters/directories",
    },
}

# Default section order, matching the original 8 H2 sections (plus the H1
# title that's added separately by the prompt). Chapters that don't declare
# a `sections:` list get this set, preserving backward-compatible behaviour
# for chapters defined before the per-chapter override existed.
_DEFAULT_SECTIONS = [
    "Quick Summary", "Architecture", "Key Data Structures", "Deep Dive",
    "Flow / Diagram", "Advanced Notes", "Comparison", "See Also",
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

    question_text = "\n".join(f"    - {q}" for q in questions)
    steps.append(
        f"STEP {step_n}: Write a README.md with three reading levels:\n"
        f"    - **Quick Summary** — 3-4 paragraphs, no code (beginners)\n"
        f"    - **Deep Dive** — source code walkthrough, struct analysis (intermediate)\n"
        f"    - **Advanced Notes** — debugging with DTrace, performance, pitfalls (advanced)\n"
        f"    - Addresses these key questions:\n{question_text}\n"
        f"    - Includes a Mermaid {diagram} diagram (see below)\n"
        f"    - References specific source files with line-level details\n"
        f"    - Connects theory (from books) to implementation (from source)"
    )

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
            "      ```"
        ),
        "class": (
            "    - Mermaid class diagram: show key structs and their relationships\n"
            "      Use: ```mermaid\\nclassDiagram\\n  class StructName {\\n    +field type\\n  }\n"
            "      ```"
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
    sections = _chapter_sections(chapter)
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

    return textwrap.dedent(f"""\
        You are writing a chapter for "FreeBSD Internals" — a
        guide that helps anyone interested in operating systems understand
        how they work by studying real FreeBSD source code.

        ## Chapter: {chapter['title']}

        ## Focus
        {focus}
        {scope_guard_block}
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

        **No marketing language.** This is a technical reference, not a
        product brochure. Forbidden words and phrases — do NOT use any of
        them, even rephrased: comprehensive, robust, seamless, seamlessly,
        leverage, leveraging, cutting-edge, state-of-the-art, elegant,
        powerful, simply, easily, effortlessly, blazing-fast, world-class,
        best-in-class, industry-leading, rich set of, wide range of,
        wide variety of, tight integration, deep integration, first-class,
        rock-solid, battle-tested, modern, sophisticated, advanced
        (as a generic adjective; the section title is fine). If a sentence
        relies on one of these words to be impressive, the sentence is
        empty — replace it with a concrete fact or delete it.
    """).lstrip()


def build_review_prompt(chapter: dict, draft: str) -> str:
    """Build the review prompt for the reviewer agent."""
    questions = chapter.get("key_questions", [])
    src_files = chapter.get("source_files", [])
    src_dirs = chapter.get("source_dirs", []) or []
    diagram = chapter.get("mermaid", "flowchart")
    question_text = "\n".join(f"- {q}" for q in questions)

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

    return textwrap.dedent(f"""\
        You are reviewing a draft chapter for "FreeBSD Internals."
        Your job is to find problems — be strict but fair.

        ## Chapter: {chapter['title']}

        ## Key Questions That Must Be Answered
        {question_text}

        ## Expected Source Files Referenced
        {chr(10).join(f"- {f}" for f in src_files)}

        {verified_block}
        ## Review Rubric

        Grade each criterion PASS / FAIL with a brief explanation:

        1. **Completeness** — Are ALL key questions above answered in the draft?
           Not hinted at — actually answered with technical detail.

        2. **Accuracy** — Does the draft reference real FreeBSD concepts correctly?
           No invented structs, no made-up function names, no wrong file paths.
           Flag anything that looks like a hallucination.
           IMPORTANT: you do NOT have direct access to the source tree.
           You CANNOT verify whether an arbitrary file path exists. Do not
           claim that a path "does not exist in the FreeBSD source tree"
           unless the path appears in the "missing paths" list above —
           paths in the "Verified Source Paths" list are confirmed to
           exist. For paths in neither list, focus on whether their *use*
           in the draft is consistent (right subsystem, right content),
           not on whether they exist.

        3. **Source Coverage** — Are the expected source files examined and
           discussed? Not just listed — actually explained with code snippets.

        4. **Mermaid Diagram** — Is there a valid Mermaid {diagram} diagram?
           Check syntax: correct keywords, no missing brackets, proper arrows.
           Does it actually illustrate the subsystem (not a generic placeholder)?

        5. **Accessibility** — Is the tone educational? Does it explain
           WHY things work, not just WHAT they do? Are there analogies or
           connections to OS theory?

        6. **Structure** — Does the draft have ALL {section_count} required
           sections with substantive content? Check for each:
{structure_checklist}
           FAIL if ANY section is missing, empty, or a single sentence.
           Sections OUTSIDE this list are not required (and not forbidden) —
           do not flag missing sections that are not on this list.

        7. **No marketing language** — FAIL if the draft contains any of
           these words or phrases (or close paraphrases): comprehensive,
           robust, seamless(ly), leverage/leveraging, cutting-edge,
           state-of-the-art, elegant, powerful, simply/easily/effortlessly,
           blazing-fast, world-class, best-in-class, industry-leading,
           rich set of, wide range of, wide variety of, tight/deep
           integration, first-class, rock-solid, battle-tested, modern,
           sophisticated. Quote the offending sentence(s) in the issue
           text so the writer can find and remove them.

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
            "mermaid_diagram": "PASS/FAIL: reason",
            "accessibility": "PASS/FAIL: reason",
            "structure": "PASS/FAIL: reason",
            "no_marketing": "PASS/FAIL: reason (quote any offending sentences)"
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


def _run_agent(agent, label: str, prompt: str) -> str:
    """Run an agent and warn if it hit its step cap.

    Hitting the cap usually means the model ran out of room to produce
    well-formed output (truncated JSON, missing sections). Surfacing it in
    the run log is the cheapest way to diagnose silent quality regressions.

    smolagents' `final_answer()` returns the raw object the agent passed
    in — which can be a dict, list, or other non-string. Every caller here
    expects a string (they call `.strip()`, write to disk, or feed into
    JSON-extraction). Coerce at this boundary so callers never have to.
    """
    result = agent.run(prompt)
    cap = getattr(agent, "max_steps", None)
    used = _agent_step_count(agent)
    if isinstance(cap, int) and isinstance(used, int) and used >= cap:
        print(f"  ⚠ {label}: hit max_steps={cap} — output may be truncated")
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
    model = OpenAIServerModel(
        model_id=MODEL_CONFIG["model_id"],
        api_base=MODEL_CONFIG["api_base"],
        api_key=MODEL_CONFIG["api_key"],
    )

    return CodeAgent(
        tools=[
            ReadFreeBSDSource(),
            SearchBooks(index),
            ExploreTree(),
            ResolveCDefinition(),
        ],
        model=model,
        additional_authorized_imports=["re", "os", "pathlib", "json"],
        max_steps=40,
    )


def create_reviewer_agent(index: TfidfIndex):
    """Create the reviewer agent — critiques drafts, no source tools needed."""
    model = OpenAIServerModel(
        model_id=MODEL_CONFIG["model_id"],
        api_base=MODEL_CONFIG["api_base"],
        api_key=MODEL_CONFIG["api_key"],
    )

    return CodeAgent(
        tools=[
            SearchBooks(index),
        ],
        model=model,
        additional_authorized_imports=["json", "re"],
        max_steps=15,
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


def _review_passes(review_json: Optional[dict]) -> bool:
    """Review gate: criteria are the canonical assessment, not `grade`/`issues`.

    History: an earlier version accepted on grade==PASS *or* empty issues,
    which silently approved drafts where individual criteria said FAIL.
    The over-correction was to also require empty issues — but that ran
    into a second failure mode where the reviewer marks all 7 criteria
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
    # 7 criteria total: completeness, accuracy, source_coverage,
    # mermaid_diagram, accessibility, structure, no_marketing.
    if not isinstance(criteria, dict):
        return 7
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
                resolve_c_definition.
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
    print(f"{'=' * 70}")

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

    success = False
    try:
        # ---- Pass 1: initial draft ----
        prompt = build_chapter_prompt(chapter)
        print("  [draft] writing initial chapter ...")

        try:
            draft = _run_agent(writer, "draft", prompt)
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
                draft = _run_agent(writer, "draft-retry", retry_prompt)
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
        # count is unchanged). best_fails starts at 8 — strictly worse
        # than any real review (max possible is 7) — so the very first
        # graded draft always wins on first comparison.
        warnings: List[str] = []
        revision = 0
        approved = False
        parse_retry_used = False
        best_draft = draft
        best_fails = 8
        best_round = 0
        last_fails: Optional[int] = None  # fail_count of the most recent graded round

        while max_revisions > 0 and revision < max_revisions:
            revision += 1
            print(f"  [review {revision}] evaluating draft ...")

            try:
                review_prompt = build_review_prompt(chapter, draft)
                review_raw = _run_agent(reviewer, f"review {revision}", review_prompt)
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
            print(f"         grade={grade}  ({7 - fail_count}/7 criteria pass)")
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
                new_draft = _run_agent(writer, f"revision {revision}", revision_prompt)
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
                      f"({7 - last_fails}/7) — using revision {best_round} "
                      f"({7 - best_fails}/7) instead")
                draft = best_draft
                warnings.append(
                    f"revisions regressed; kept revision {best_round} "
                    f"({7 - best_fails}/7 criteria) over revision {revision} "
                    f"({7 - last_fails}/7)"
                )

        # ---- Fact-checking pass ----
        print("  [fact-check] verifying paths, structs, funcs, options, "
              "dtrace probes ...")
        facts = fact_check_draft(draft, SRC_ROOT)
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
            if facts.get('kernel_options_not_found'):
                print(f"         - missing kernel options: "
                      f"{', '.join(facts['kernel_options_not_found'])}")
            if facts.get('dtrace_probes_not_found'):
                print(f"         - missing dtrace probes: "
                      f"{', '.join(facts['dtrace_probes_not_found'])}")

            print("  [fact-fix] rewriting to address fact-check issues ...")
            try:
                fact_prompt = _build_fact_check_prompt(chapter, draft, facts)
                new_draft = _run_agent(writer, "fact-fix", fact_prompt)
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


def _extract_json(text: str) -> Optional[dict]:
    """Extract a JSON object from LLM output (may have prose around it)."""
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
                return json.loads(text[start:i+1])
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
    # Linux structs / funcs that legitimately appear in Comparison sections
    # (they don't exist in the FreeBSD tree, but flagging them as
    # "missing" wastes a fact-fix step).
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
_COMPARISON_SECTION_RE = re.compile(
    r'^[ ]{0,3}##\s+Comparison\b.*?(?=^[ ]{0,3}##\s+|\Z)',
    re.MULTILINE | re.DOTALL,
)


def _strip_comparison_section(text: str) -> str:
    """Return `text` with all `## Comparison` H2 sections removed.

    Used by the fact-checker so cross-OS struct/function names are not
    grepped against the FreeBSD source tree and flagged as missing.
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
    """Extract claimed struct names from markdown text."""
    structs = []
    # Match patterns like "struct vm_page", "struct vnode", "struct foo_bar"
    for m in re.finditer(r'\bstruct\s+([a-zA-Z_]\w*)\b', text):
        name = m.group(1)
        # Skip common non-struct words
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
    return _filter_known_noise(list(funcs))


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
        # Try glob fallback
        pattern = os.path.join(src_root, p.split('/')[0], '**', p.split('/')[-1])
        matches = list(glob.glob(pattern, recursive=True))
        # Filter to candidates that actually exist on disk. Glob already
        # returns existing entries, but be defensive against symlinks or
        # races between glob and the os.path.exists() recheck below.
        candidate = next(
            (m for m in matches if os.path.exists(m) and os.path.isfile(m)),
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
# the total cost.
_GREP_TIMEOUT_SEC = 8


def _batched_grep_present(symbols: List[str], pattern_template: str,
                          search_root: str, shape_grep: str) -> set:
    """Run one grep over `search_root` looking for any of `symbols`.

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

    # Stage 1: fast fixed-string grep for any of the symbols. `-w` keeps
    # us from matching substrings (e.g. `proc` inside `procfs`).
    # Stage 2: shape filter so the 1 MB cap holds candidate definitions.
    fixed_args = " ".join(f"-e {shlex.quote(s)}" for s in symbols)
    cmd = (
        f"grep -rhwF --include='*.c' --include='*.h' {fixed_args} "
        f"{shlex.quote(search_root)}/ 2>/dev/null | "
        f"grep -E {shlex.quote(shape_grep)} | "
        f"head -c 1048576"
    )
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=_GREP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        # Conservative behaviour: treat a timeout as "verification not
        # available" — return empty set so callers report all symbols as
        # not-found rather than silently approving them. The fact-check
        # is itself best-effort, but a silent timeout would mask real
        # hallucinations, which is the failure mode we care most about.
        print(f"  ⚠ fact-check grep timed out after {_GREP_TIMEOUT_SEC}s — "
              f"treating {len(symbols)} symbol(s) as unverified")
        return set()

    output = result.stdout
    if not output:
        return set()

    # Stage 2: validate the shape per symbol with Python re.
    matched = set()
    for s in symbols:
        py_pattern = pattern_template.format(alt=re.escape(s))
        if re.search(py_pattern, output):
            matched.add(s)
    return matched


def _verify_with_cache(kind: str, symbols: List[str], src_root: str,
                       pattern_template: str, shape_grep: str) -> List[str]:
    """Common path for struct/function verification.

    Splits `symbols` into already-cached and uncached, runs one batched
    grep over the uncached set, updates the cache, then returns the list
    of symbols that are not present in the source tree.
    """
    search_root = os.path.join(src_root, "sys")
    uncached = []
    not_found = []
    for s in symbols:
        cached = _FACT_CHECK_CACHE.get((kind, src_root, s))
        if cached is True:
            continue
        if cached is False:
            not_found.append(s)
            continue
        uncached.append(s)

    if uncached:
        present = _batched_grep_present(
            uncached, pattern_template, search_root, shape_grep,
        )
        for s in uncached:
            present_now = s in present
            _FACT_CHECK_CACHE[(kind, src_root, s)] = present_now
            if not present_now:
                not_found.append(s)

    return not_found


def _verify_structs(structs: List[str], src_root: str) -> List[str]:
    """Verify that claimed struct names exist in the source tree.

    Returns a list of struct names that could not be found.
    Backed by `_FACT_CHECK_CACHE` so re-runs within a session are free.
    """
    # Match "struct NAME {" — the canonical struct definition shape.
    # `pattern_template` is for Python re; `shape_grep` is the BSD-grep
    # filter that keeps only candidate definition lines so the 1 MB cap
    # holds them.
    return _verify_with_cache(
        "struct", structs, src_root,
        pattern_template=r"struct\s+({alt})\s*\{{",
        shape_grep=r"^struct [A-Za-z_][A-Za-z0-9_]* *\{",
    )


def _verify_functions(funcs: List[str], src_root: str) -> List[str]:
    """Verify that claimed function names exist in the source tree.

    Returns a list of function names that could not be found.
    Backed by `_FACT_CHECK_CACHE` so re-runs within a session are free.
    """
    # Match common return-type prefixes followed by NAME(. The pattern is
    # used by the Python re re-scan (not by grep), so we use the
    # non-capturing form for the type-token alternation — only the symbol
    # name needs a capture (none of which the caller actually consumes).
    # `\s+` and `\*?` widen coverage to pointer-returning functions like
    # `void *malloc(` that the previous per-symbol regex missed.
    type_tokens = (
        r"(?:void|int|static|struct|enum|uint|char|u_int|u_char|error_t)"
    )
    pattern = type_tokens + r"\s+\*?\s*({alt})\s*\("
    # `shape_grep` keeps lines that look like a function signature:
    # an identifier followed (optionally through `*` and spaces) by `(`.
    # That filters out includes/comments/string literals but accepts
    # both `void malloc(` and `void *malloc(`. The Python re re-scan
    # then tightens this to a type-token + name shape.
    return _verify_with_cache(
        "func", funcs, src_root,
        pattern_template=pattern,
        shape_grep=r"[A-Za-z_][A-Za-z0-9_]* *\*? *[A-Za-z_][A-Za-z0-9_]*\(",
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
    fixed_args = " ".join(f"-e {shlex.quote(s)}" for s in options)
    cmd = (
        f"grep -rhwF "
        f"--include='options' --include='options.*' --include='NOTES' "
        f"{fixed_args} {shlex.quote(sys_conf)}/ 2>/dev/null | "
        f"head -c 524288"
    )
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=_GREP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return []  # treat as unverified rather than all-missing
    output = result.stdout
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
    # few hundred SDT probes total.
    cmd = (
        f"grep -rhE --include='*.c' --include='*.h' "
        f"'SDT_PROBE_DEFINE[0-9]?\\b' {shlex.quote(sys_root)}/ 2>/dev/null | "
        f"head -c 1048576"
    )
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=_GREP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return []
    output = result.stdout
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


def fact_check_draft(draft: str, src_root: str) -> dict:
    """Run structured fact-checking on a draft chapter.

    Returns a dict with:
        - 'file_paths_not_found': list of missing paths
        - 'file_paths_corrected': list of "wrong → right" pairs
        - 'structs_not_found': list of missing struct names
        - 'funcs_not_found': list of missing function names
        - 'kernel_options_not_found': list of unverifiable kernel-config options
        - 'dtrace_probes_not_found': list of unverifiable DTrace SDT probes
        - 'total_issues': count of all issues

    The `## Comparison` section is stripped before extraction. That section
    legitimately discusses Linux/macOS/NetBSD symbols which would otherwise
    be flagged as "not found in FreeBSD source," wasting fact-fix steps.
    """
    fact_text = _strip_comparison_section(draft)
    file_paths = _extract_file_paths(fact_text)
    structs = _extract_struct_names(fact_text)
    funcs = _extract_function_names(fact_text)
    kernel_options = _extract_kernel_options(fact_text)
    dtrace_probes = _extract_dtrace_probes(fact_text)

    paths_missing = _verify_file_paths(file_paths, src_root)
    paths_corrected = [x for x in paths_missing if ' → ' in x]
    paths_missing = [x for x in paths_missing if ' → ' not in x]

    structs_missing = _verify_structs(structs, src_root)
    funcs_missing = _verify_functions(funcs, src_root)
    kernel_options_missing = _verify_kernel_options(kernel_options, src_root)
    dtrace_probes_missing = _verify_dtrace_probes(dtrace_probes, src_root)

    return {
        'file_paths_not_found': paths_missing,
        'file_paths_corrected': paths_corrected,
        'structs_not_found': structs_missing,
        'funcs_not_found': funcs_missing,
        'kernel_options_not_found': kernel_options_missing,
        'dtrace_probes_not_found': dtrace_probes_missing,
        'total_issues': (len(paths_missing) + len(paths_corrected) +
                         len(structs_missing) + len(funcs_missing) +
                         len(kernel_options_missing) +
                         len(dtrace_probes_missing)),
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
    if facts['funcs_not_found']:
        issues.append(
            f"Functions not found in source tree: "
            f"{', '.join(facts['funcs_not_found'])}. "
            f"Remove or correct these with real function signatures."
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


# Chapter relationship map — which chapters reference each other
CHAPTER_RELS = {
    "FreeBSD Source Tree Overview": ["The FreeBSD Kernel — Structure and Entry Point"],
    "The FreeBSD Kernel — Structure and Entry Point": [
        "FreeBSD Source Tree Overview",
        "UEFI Bootloader-to-Kernel Handoff",
        "Process Management and Scheduling",
    ],
    "UEFI Bootloader-to-Kernel Handoff": [
        "The FreeBSD Kernel — Structure and Entry Point",
        "The FreeBSD Build System",
    ],
    "Virtual Memory Subsystem": [
        "The FreeBSD Kernel — Structure and Entry Point",
        "The Buffer Cache and I/O Subsystem",
        "Virtual File System (VFS) Layer",
    ],
    "Process Management and Scheduling": [
        "The FreeBSD Kernel — Structure and Entry Point",
        "Interrupt Handling",
        "Jails and System Isolation",
    ],
    "The Buffer Cache and I/O Subsystem": [
        "Virtual Memory Subsystem",
        "Virtual File System (VFS) Layer",
        "UFS Filesystem Implementation",
    ],
    "Virtual File System (VFS) Layer": [
        "Virtual Memory Subsystem",
        "The Buffer Cache and I/O Subsystem",
        "UFS Filesystem Implementation",
        "Network Stack Architecture",
    ],
    "UFS Filesystem Implementation": [
        "Virtual File System (VFS) Layer",
        "The Buffer Cache and I/O Subsystem",
    ],
    "Network Stack Architecture": [
        "Virtual File System (VFS) Layer",
        "Device Driver Framework",
    ],
    "Device Driver Framework": [
        "Network Stack Architecture",
        "Interrupt Handling",
    ],
    "Interrupt Handling": [
        "Device Driver Framework",
        "Process Management and Scheduling",
    ],
    "Jails and System Isolation": [
        "Process Management and Scheduling",
        "The FreeBSD Kernel — Structure and Entry Point",
    ],
    "The FreeBSD Build System": [
        "UEFI Bootloader-to-Kernel Handoff",
        "FreeBSD Source Tree Overview",
    ],
}


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

    updated = {}

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
        related = CHAPTER_RELS.get(title, [])

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

        # Build the sidebar
        sidebar_lines = [
            _AUTO_GEN_BANNER,
            "",
            "---",
            "**Navigation:**",
        ]
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
        content = _add_see_also_links(content, title, title_map, output_file)

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
                        current_file: str) -> str:
    """Add cross-links to the See Also section of a README."""
    related = CHAPTER_RELS.get(title, [])
    if not related:
        return content

    # Find the See Also section
    see_also_idx = content.find("\n## See Also\n")
    if see_also_idx == -1:
        see_also_idx = content.find("\n## See Also")
    if see_also_idx == -1:
        return content

    # Build cross-links
    links = []
    for rel_title in related:
        rel_file = title_map.get(rel_title)
        if rel_file and rel_file != current_file:
            rel_dir = os.path.dirname(rel_file) or "."
            if rel_dir == ".":
                links.append(f"[{rel_title}]({os.path.basename(rel_file)})")
            else:
                parts = rel_dir.split("/")
                depth = len(parts) + 1
                prefix = "../" * depth
                links.append(f"[{rel_title}]({prefix}{rel_file})")

    if not links:
        return content

    # Insert links after the See Also header
    insert_pos = see_also_idx + len("## See Also")
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

    # Group chapters by relationship
    for title, related in CHAPTER_RELS.items():
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


if __name__ == "__main__":
    main()
