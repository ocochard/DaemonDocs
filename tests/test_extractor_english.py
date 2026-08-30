#!/usr/bin/env python3
"""Tests for the extractor English-word / man-page-citation filters.

The grep-cap fix (2026-08-29) stopped `_batched_grep_present` from
silently truncating and poisoning the cache, which was ch34's failure.
ch40 (USB/Thunderbolt, 2026-08-30) then shipped UNVERIFIED for the
*next* defect in the same family: the extractors themselves handed the
reviewer names the draft never claimed were symbols.

Two distinct shapes, both from real shipped chapters:

  1. `struct` used as an English NOUN, not the C keyword. ch40 produced
     `struct above` ("the struct above"), `struct defines` ("the
     on-the-wire struct defines") and `struct tree` ("a four-struct
     tree" -- `\\b` matches inside the hyphenated compound). A sweep of
     all 56 shipped chapters found three more: `avoids`, `allocated`,
     `rather`.

  2. Man-page citations. `name(N)` is indistinguishable from a
     zero-or-one-arg call, so a See Also list of `usbdi(9)`, `usb(4)`,
     `newbus(4)` arrived as three "claimed functions".

Both fed the reviewer's "Missing structs/functions" lists, which the
Accuracy criterion is explicitly told to FAIL on. The reviewer was
following its rubric correctly against fabricated input -- the same
mechanism as ch34, one layer upstream.

What is pinned here:
  1. the six observed English-noun struct cases are dropped;
  2. real struct tags that ARE ordinary words still verify;
  3. man-page citations are dropped, real calls are not;
  4. every `_ENGLISH_AFTER_STRUCT` entry is safe (never a real tag);
  5. the ch40 regression, end to end on its real shipped text;
  6. function words that ARE real kernel definitions are NOT
     denylisted -- suppressing them would hide real hallucinations.

Run: `python3 test_extractor_english.py`. Exits non-zero on failure.
"""
import importlib.util
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "gendoc", os.path.join(REPO, "generate-doc.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

SRC = os.path.expanduser("~/freebsd-src")
failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")
    if not ok:
        failures.append(label)


print("1) `struct` as an English noun is not a struct name")
# Every string below is copied from a shipped chapter, not invented.
ENGLISH_CASES = [
    ("the struct above", "above",
     "ch40: 'building a `tb_cfg_read` frame (the struct above)'"),
    ("the same fields the on-the-wire struct defines.", "defines",
     "ch40: packs the same fields the on-the-wire struct defines"),
    ("a four-struct tree", "tree",
     "ch40: hyphenated compound -- \\b matched inside 'four-struct'"),
    ("one struct avoids repeated walking", "avoids",
     "nfs: 'one struct avoids repeated `mbuf` walking'"),
    ("a distinct struct allocated by its own helper", "allocated",
     "nic_drivers: 'a distinct struct allocated by its own ...'"),
    ("the argument struct rather than typed by hand", "rather",
     "syscall: 'derived from the generated argument struct rather ...'"),
]
for prose, word, why in ENGLISH_CASES:
    got = mod._extract_struct_names(prose)
    check(f"'{word}' not extracted from: {prose[:40]}...",
          word not in got, f"{why}; got {got}")

print()
print("2) real struct tags that are ordinary English words still verify")
# The denylist must never swallow these -- FreeBSD names many core
# types with plain words. If any of these stops extracting, the
# fact-checker has gone blind to a whole class of real symbol.
for tag in ("buf", "file", "proc", "thread", "mount", "socket",
            "vnode", "session", "lock", "task", "disk", "device",
            "resource", "module", "jail", "prison", "domain",
            "witness", "stack", "label", "pipe", "adapter"):
    got = mod._extract_struct_names(f"See `struct {tag}` for details.")
    check(f"struct {tag} extracted", tag in got, f"got {got}")

print()
print("3) man-page citations are not function calls")
seealso = (
    "- **Man pages** -- [`usbdi(9)`](../../../share/man/man9/usbdi.9), "
    "`usb(4)`, `newbus(4)`, `bus_dma(9)`, `device(9)`, `mbuf(9)`.\n"
)
got = mod._extract_function_names(seealso)
for cite in ("usbdi", "usb", "newbus", "bus_dma", "device", "mbuf"):
    check(f"`{cite}(N)` not a claimed function", cite not in got,
          f"got {sorted(got)}")

# ...but a real call with real arguments still is.
calls = (
    "The driver calls `xhci_interrupt(sc)` and then "
    "`usb_process(&sc->sc_bus, USB_PROC_EXPLORE)`. "
    "`malloc(size, M_USB, M_WAITOK)` allocates it.\n"
)
got = mod._extract_function_names(calls)
for real in ("xhci_interrupt", "usb_process", "malloc"):
    check(f"{real}() still extracted", real in got, f"got {sorted(got)}")

# A single *numeric* arg is the ambiguous case the section-digit test
# resolves. `free(9)` is a man page; `free(mem)` is a call.
check("`free(9)` treated as a citation",
      "free" not in mod._extract_function_names("see `free(9)` for this"),
      "bare section digit => citation")
check("`free(ptr)` treated as a call",
      "free" in mod._extract_function_names("it calls `free(ptr)` at exit"),
      "named argument => real call")

print()
print("4) every denylist entry is safe (never a real struct tag)")
# The comment beside `_ENGLISH_AFTER_STRUCT` claims no entry is a real
# FreeBSD struct tag. That claim is load-bearing -- if it were wrong,
# the fix would blind the checker to a real type. Verify it, don't
# trust it. Skipped when the source tree is absent.
if os.path.isdir(os.path.join(SRC, "sys")):
    words = sorted(mod._ENGLISH_AFTER_STRUCT)
    # One grep per batch keeps BSD grep -E out of its nested-alternation
    # pathology (the same reason the fact-checker greps in two stages).
    collisions = {}
    BATCH = 25
    for i in range(0, len(words), BATCH):
        pat = "|".join(words[i:i + BATCH])
        r = subprocess.run(
            ["grep", "-rhoE",
             r"(^|[^[:alnum:]_])struct[ \t]+(" + pat + r")\b[ \t]*\{",
             "sys/"],
            cwd=SRC, capture_output=True, text=True,
            errors="replace", timeout=600)
        for line in r.stdout.splitlines():
            w = re.sub(r".*struct\s+", "", line).rstrip(" \t{")
            if w in mod._ENGLISH_AFTER_STRUCT:
                collisions[w] = collisions.get(w, 0) + 1
    check(f"no denylist word is a real struct tag ({len(words)} checked)",
          not collisions, f"collisions: {collisions}" if collisions
          else "clean")
else:
    print(f"  [SKIP] {SRC}/sys not present")

print()
print("5) the ch40 regression, on its real shipped text")
ch40 = os.path.join(SRC, "sys/dev/usb/README.md")
if os.path.exists(ch40):
    text = open(ch40, encoding="utf-8", errors="replace").read()
    structs = mod._extract_struct_names(text)
    funcs = mod._extract_function_names(text)
    # These six are exactly what the reviewer was told to FAIL on.
    for bad in ("above", "defines", "tree"):
        check(f"ch40: struct {bad} gone", bad not in structs)
    for bad in ("newbus", "usb", "usbdi"):
        check(f"ch40: {bad}() gone", bad not in funcs)
    # And the chapter's real types must survive the change.
    for good in ("usb_host_endpoint", "xhci_softc", "usb_xfer",
                 "nhi_cmd_frame", "router_softc"):
        check(f"ch40: struct {good} kept", good in structs,
              f"got {sorted(structs)}")
    check("ch40 extracts a plausible number of structs",
          15 <= len(structs) <= 40, f"got {len(structs)}")
else:
    print(f"  [SKIP] {ch40} not present")

print()
print("6) real kernel functions are NOT denylisted")
# `idle` has two definition-shaped hits in sys/ (ffs_softdep.c,
# geom_event.c). Denylisting the words that showed up as `acked()` /
# `idle()` prose in README_transport would suppress a real
# hallucination signal -- the writer inventing `acked()` as a
# pseudo-call IS a defect the fact-checker should report. This test
# pins that decision so nobody "tidies" these into the ignore list.
for word in ("idle", "destroy", "acked", "recovery"):
    got = mod._extract_function_names(f"the state machine calls `{word}()`")
    check(f"{word}() still reported as a claim", word in got,
          "must stay visible to the verifier")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("all extractor-english tests passed")
