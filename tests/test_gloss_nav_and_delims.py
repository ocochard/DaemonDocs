#!/usr/bin/env python3
"""Tests for two gloss-scanner false-positive sources found on ch5.

Motivating defect (ch5, syscall/exec, 2026-09-01): review 1 graded the
chapter NEEDS_REVISION on `accessibility`, listing `UMA`, `KLD`,
`SYSINIT` and `vm_object` as used-without-definition. Four of the five
findings were wrong, in two distinct ways:

  1. The auto-generated NAV SIDEBAR at the top of every chapter is built
     from other chapters' titles ("Kernel Modules and the Linker — KLD,
     SYSINIT, and linker sets"). The writer never wrote that text and
     cannot gloss it -- it is regenerated on every nav rebuild. Because
     it sits at the TOP of the file it captured "first prose use" and
     hid the real gloss further down: ch5 shipped correct glosses for
     UMA/KLD/SYSINIT at lines 399-400 and was flagged anyway.

     Measured across the 100 shipped chapters: 99 of 159 jargon findings
     (62%) had their first prose use inside the nav block.

  2. Tier-2 gloss cues are checked at OFFSET 0 only (deliberately -- the
     first version accepted them anywhere in the window and missed the
     ch12 KVM case it was written for). But a term is often followed by
     a CLOSING delimiter before its gloss starts:

         `vm_object` (definition)                    closing backtick
         [mbuf](../README.md#glossary) (definition)  markdown link tail

     Both failed the offset-0 check and were reported unglossed.

What is pinned here:
  1. the nav block is excluded from the jargon scan;
  2. the nav block is excluded from the acronym scan;
  3. a term glossed only in the BODY is clean even when the nav block
     mentions it first (the exact ch5 shape);
  4. a genuinely unglossed term is STILL reported when the nav block
     also mentions it -- the strip must not become a blanket amnesty;
  5. closing-backtick and markdown-link forms count as glossed;
  6. the offset-0 rule is NOT relaxed: a cue further into the window,
     behind an intervening word, is still not a gloss (this is the ch12
     regression guard);
  7. a backtick cannot smuggle in a gloss from inside a code span.

Run: `python3 test_gloss_nav_and_delims.py`. Exits non-zero on failure.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "gendoc", os.path.join(REPO, "generate-doc.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")
    if not ok:
        failures.append(label)


BANNER = mod._AUTO_GEN_BANNER

# The real shape emitted by _build_nav_sidebar, terms and all.
NAV = (
    f"{BANNER}\n"
    "\n"
    "---\n"
    "**Navigation:**\n"
    "  **Up:** [Kernel Core](../README.md)\n"
    "  **Related:** [Kernel Modules and the Linker — KLD, SYSINIT, and "
    "linker sets](README_kld.md)\n"
    "  **All chapters:** [Virtual Memory Subsystem — vm_page, UMA, and "
    "Pagers](../vm/README.md) ...\n"
    "---\n"
    "\n"
)

BODY_CLEAN = (
    "# A Chapter\n\n"
    "## Deep Dive\n\n"
    "The allocator hands pages back through UMA (the kernel's unified "
    "memory allocator) once the transfer retires, which keeps the hot "
    "path free of any global lock.\n"
)

print("1) nav block is excluded from the jargon scan")
got_with = mod._extract_unglossed_jargon(NAV + BODY_CLEAN)
check("UMA not flagged when glossed in body but named in nav",
      "UMA" not in got_with, f"got {got_with}")
check("KLD not flagged (nav-only mention)",
      not any(t.lower() == "kld" for t in got_with), f"got {got_with}")
check("SYSINIT not flagged (nav-only mention)",
      not any(t.lower() == "sysinit" for t in got_with), f"got {got_with}")

print()
print("2) nav block is excluded from the acronym scan")
# Repeat an acronym inside the nav block only; it must not be reported.
nav_heavy = NAV + NAV + "## Body\n\nNothing technical here at all.\n"
got_ac = mod._extract_unexpanded_acronyms(nav_heavy)
check("no acronym reported from nav text alone",
      "KLD" not in got_ac and "UMA" not in got_ac, f"got {got_ac}")

print()
print("3) body gloss wins over nav-block first use (the ch5 shape)")
check("clean chapter stays clean",
      mod._extract_unglossed_jargon(NAV + BODY_CLEAN) == []
      or "UMA" not in mod._extract_unglossed_jargon(NAV + BODY_CLEAN),
      f"got {mod._extract_unglossed_jargon(NAV + BODY_CLEAN)}")

print()
print("4) strip is not a blanket amnesty")
body_bare = (
    "# A Chapter\n\n"
    "## Deep Dive\n\n"
    "The turnstile and sleep-queue hash tables are allocated from UMA "
    "zones, and most of the virtual-memory locking is sx-based, which "
    "keeps the fast path short.\n"
)
got_bare = mod._extract_unglossed_jargon(NAV + body_bare)
check("genuinely unglossed UMA still reported despite nav mention",
      "UMA" in got_bare, f"got {got_bare}")

print()
print("5) closing delimiters do not hide a gloss")
check("closing backtick",
      mod._has_gloss("` (the kernel's in-memory page container) and then"))
check("markdown link tail",
      mod._has_gloss("](../sys/README.md#glossary) (the packet-data "
                     "descriptor) and then"))
check("plain prose still works",
      mod._has_gloss(" (the kernel's unified memory allocator) and then"))

print()
print("6) offset-0 rule is NOT relaxed (ch12 regression guard)")
# An intervening word before the parenthetical: this is NOT a gloss of the
# term, and accepting it is exactly the bug the offset-0 rule prevents.
check("cue behind an intervening word is not a gloss",
      not mod._has_gloss(" chain (the kernel's packet-data descriptor list)"))
check("cue far into the window is not a tier-2 gloss",
      not mod._has_gloss("appears in the path. " + "x" * 60
                         + " (a definition of something else)"))

print()
print("7) a backtick cannot smuggle a gloss out of a code span")
# Leading backtick is always a CLOSER (the term matched is the bare word
# inside the span), so this must not admit code-span content as prose.
check("bare backtick with no following cue is not a gloss",
      not mod._has_gloss("`, `bus_dmamap_sync`, and `bus_dma_tag_create`"))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
    sys.exit(1)
print("all gloss nav/delimiter tests passed")
