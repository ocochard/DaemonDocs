#!/usr/bin/env python3
"""Tests for the deterministic glossary first-use linker (step 2).

Step 1 taught the reviewer to *complain* about undefined jargon. It did
not help much on its own: measured across the five chapters shipped the
week of 2026-08-22, 5.2 undefined terms per chapter, and 0 of 5 carried
a Glossary at all. Step 2 is the other half — `Glossary` becomes a
default section so the terms have somewhere to live, and this linker
turns the first prose use of each term into a link to its definition.

The linker is the fourth sibling to `_link_see_also_source_paths` and
`_link_manpage_refs` in phase 4, and inherits their rules: mask fenced
blocks, never rewrite inside an existing link, be idempotent.

What is pinned here:
  1. glossary parsing accepts both corpus shapes (bullet and bare);
  2. only the FIRST prose use is linked, not every mention;
  3. inline code, existing links, headings and the Glossary section
     itself are never rewritten;
  4. fenced code blocks are never rewritten;
  5. cross-chapter links use a path derived from `output_file`, not an
     assumed depth (the ch17 depth bug);
  6. a local definition beats an external one for the same term;
  7. running it twice changes nothing (idempotent);
  8. the real sys/vm/README.md glossary parses to the terms it defines.

Run: `python3 test_glossary_linker.py`. Exits non-zero on any failure.
"""
import importlib.util
import os
import re
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


# A chapter with a Glossary in the templated (bare) shape.
BARE = (
    "# Test Chapter\n\n"
    "## Quick Summary\n\n"
    "Something about the subsystem.\n\n"
    "## Glossary\n\n"
    "**Slab** — A contiguous run of pages managed by UMA.\n"
    "**TLB shootdown** — An IPI that invalidates stale translations.\n\n"
    "## Deep Dive\n\n"
    "The allocator carves a slab out of the free pool. A second slab is "
    "requested later, and a third slab after that.\n"
)

# The same, in the bullet shape the one real corpus chapter uses.
BULLET = BARE.replace("\n**Slab**", "\n- **Slab**").replace(
    "\n**TLB shootdown**", "\n- **TLB shootdown**")


print("1) glossary parsing accepts both corpus shapes")
for label, text in (("bare `**term** —`", BARE), ("bullet `- **term** —`", BULLET)):
    got = mod._parse_glossary_entries(text)
    check(f"{label}: both terms found",
          set(got) == {"Slab", "TLB shootdown"}, f"got {sorted(got)}")
    # Every term targets the Glossary SECTION, not a per-term anchor.
    # GitHub only mints anchors for headings; a bolded list item gets
    # none, so `#tlb-shootdown` would resolve to nothing. Caught by
    # checking the real sys/vm/README.md headings on 2026-08-29.
    check(f"{label}: term targets the glossary section",
          got.get("TLB shootdown") == "glossary", f"got {got}")

# A hyphen separator is deliberately NOT a definition — an ordinary
# bullet list whose item starts bold would otherwise parse as glossary.
notgloss = ("## Glossary\n\n- **Slab** - not a real definition line\n")
check("hyphen separator is not treated as a definition",
      mod._parse_glossary_entries(notgloss) == {},
      f"got {mod._parse_glossary_entries(notgloss)}")

print()
print("2) only the first prose use is linked")
out, n = mod._link_glossary_first_use(BARE, "sys/vm/README.md", {})
check("two terms defined, one used in prose -> 1 link", n == 1, f"n={n}")
check("first use linked", "[slab](#glossary)" in out, out[-260:])
check("later uses untouched", out.count("](#glossary)") == 1,
      f"count={out.count('](#glossary)')}")

print()
print("3) code, links, headings and the Glossary are skipped")
tricky = (
    "# Chapter\n\n"
    "## Glossary\n\n"
    "**Slab** — A contiguous run of pages.\n\n"
    "## Slab Handling\n\n"
    "The `slab` field is set first. See [slab docs](other.md) for more. "
    "Then the slab is handed to the caller.\n"
)
out, n = mod._link_glossary_first_use(tricky, "sys/vm/README.md", {})
check("inline code not linked", "[`slab`]" not in out and "`slab`" in out)
check("existing link not rewritten", "[slab docs](other.md)" in out)
check("heading not rewritten", "## Slab Handling" in out)
check("definition inside Glossary not self-linked",
      "**Slab** — A contiguous run of pages." in out)
check("the one bare prose use is linked", n == 1, f"n={n}")

print()
print("3b) See Also and quoted phrases are skipped")
# Both defects were found by dry-running phase 4 over the real corpus on
# 2026-08-29, not by imagining inputs — See Also is already a link list
# owned by three other linkers, and `a "drop zone"` in README_internals
# is ordinary English, the corpus's one homograph false positive.
seealso = (
    "## Glossary\n\n**Zone** — The UMA allocation interface.\n\n"
    "## See Also\n\n- **VM chapter:** `vm/uma.h` — zone allocation\n"
)
out, n = mod._link_glossary_first_use(seealso, "sys/kern/README.md", {})
check("nothing linked inside See Also", n == 0, f"n={n}")

quoted = (
    "## Glossary\n\n**Zone** — The UMA allocation interface.\n\n"
    "## Deep Dive\n\n"
    "Code is vendored into a \"drop zone\" and integrated later.\n"
)
out, n = mod._link_glossary_first_use(quoted, "README.md", {})
check("quoted phrase not linked", n == 0, f"n={n}")
check("quote left intact", '"drop zone"' in out)

# But an unquoted use in the same chapter is still linked.
mixed = quoted.replace(
    "and integrated later.",
    "and integrated later. The zone is then built.")
out, n = mod._link_glossary_first_use(mixed, "README.md", {})
check("unquoted use after a quoted one is linked",
      n == 1 and "The [zone](#glossary) is then built." in out, f"n={n}")

print()
print("4) fenced code blocks are never rewritten")
fenced = (
    "## Glossary\n\n"
    "**Slab** — A contiguous run of pages.\n\n"
    "## Deep Dive\n\n"
    "```c\n"
    "/* slab is allocated here */\n"
    "struct slab *s = alloc();\n"
    "```\n\n"
    "Afterwards the slab is returned.\n"
)
out, n = mod._link_glossary_first_use(fenced, "sys/vm/README.md", {})
check("code block untouched", "struct slab *s = alloc();" in out)
check("comment inside fence untouched", "/* slab is allocated here */" in out)
check("prose after the fence is what got linked",
      "the [slab](#glossary) is returned" in out, out[-120:])

print()
print("5) cross-chapter links derive depth from output_file")
# The ch17 bug: a chapter one directory deeper needs one more `../`.
# Nothing may assume a fixed depth.
consumer = (
    "# Consumer\n\n## Deep Dive\n\nThe keg owns a set of slabs.\n"
)
index = {"Keg": ("sys/vm/README.md", "glossary")}
for out_file, want in (
    ("sys/kern/README.md", "../vm/README.md#glossary"),
    ("sys/dev/usb/README.md", "../../vm/README.md#glossary"),
    ("README.md", "sys/vm/README.md#glossary"),
):
    out, n = mod._link_glossary_first_use(consumer, out_file, index)
    check(f"{out_file} -> {want}", f"({want})" in out,
          out.strip().splitlines()[-1])

# A chapter never links to itself across files.
out, n = mod._link_glossary_first_use(
    consumer, "sys/vm/README.md", index)
check("no self-referential cross-chapter link", n == 0, f"n={n}")

print()
print("6) a local definition beats an external one")
local = (
    "## Glossary\n\n**Keg** — Defined right here.\n\n"
    "## Deep Dive\n\nThe keg owns slabs.\n"
)
out, n = mod._link_glossary_first_use(
    local, "sys/kern/README.md", {"Keg": ("sys/vm/README.md", "glossary")})
check("links to the local anchor", "[keg](#glossary)" in out, out[-90:])
check("does not link across files", "vm/README.md#glossary" not in out)

print()
print("7) idempotent")
once, n1 = mod._link_glossary_first_use(BARE, "sys/vm/README.md", {})
twice, n2 = mod._link_glossary_first_use(once, "sys/vm/README.md", {})
check("second pass adds nothing", n2 == 0, f"n2={n2}")
check("content unchanged by second pass", once == twice)

print()
print("8) the real sys/vm/README.md glossary (frozen fixture)")
# Pinned against real shipped text, not a synthetic sample: this is the
# only chapter in the corpus that carried a Glossary before step 2, and
# it is the reason the parser accepts the bullet shape at all.
#
# The text is a FROZEN COPY, not the live artifact. Reading
# ~/freebsd-src/sys/vm/README.md directly made this suite fail whenever
# ch7 regenerated that chapter, for two reasons that are both properties
# of the pipeline working correctly, not bugs:
#
#   1. The glossary's contents are model-authored, so which eight terms
#      it defines drifts between runs. A 2026-09-02 regen swapped `PML4`
#      for `PV list` and this block started failing on a chapter that was
#      perfectly fine.
#   2. Phase 4 links the file on its way out, so the shipped artifact
#      already carries its glossary links. Asserting `n > 0` on it
#      asserts that re-linking an already-linked file does more work --
#      the exact opposite of the idempotence invariant group 7 pins.
#
# Freezing keeps the "real shipped text" property that motivated the
# block while removing the dependency on a file another chapter owns.
real = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "fixtures", "vm_README.snapshot.md")
if os.path.exists(real):
    with open(real, errors="replace") as f:
        rtext = f.read()
    got = mod._parse_glossary_entries(rtext)
    for term in ("Buddy allocator", "Slab", "Keg", "Zone",
                 "Shadow chain", "TLB shootdown", "PV list", "NUMA domain"):
        check(f"parsed: {term}", term in got, f"got {sorted(got)}")
    check("no spurious terms beyond the 8 defined",
          len(got) == 8, f"got {len(got)}: {sorted(got)}")

    # The fixture ships already-linked (phase 4 ran before it was
    # frozen), so linking it again must be a no-op. Strip the links
    # first to exercise the produces-links path on real prose.
    unlinked = re.sub(r"\[([^\]]+)\]\(#glossary\)", r"\1", rtext)
    check("fixture really was already linked",
          unlinked != rtext, "no (#glossary) links found to strip")
    out, n = mod._link_glossary_first_use(unlinked, "sys/vm/README.md", {})
    check("linking the real chapter produces links", n > 0, f"n={n}")
    out2, n2 = mod._link_glossary_first_use(out, "sys/vm/README.md", {})
    check("real chapter is idempotent too", n2 == 0 and out == out2,
          f"n2={n2}")
    # And the shipped form is a fixed point, which is what phase 4
    # actually guarantees about its own output.
    _, n3 = mod._link_glossary_first_use(rtext, "sys/vm/README.md", {})
    check("shipped (already-linked) form is a fixed point", n3 == 0,
          f"n3={n3}")
else:
    # A committed fixture is missing -- that is a repo defect, not an
    # environment difference, so fail instead of skipping quietly.
    check("fixture vm_README.snapshot.md present", False, f"missing: {real}")

print()
print("9) every emitted anchor corresponds to a real heading")
# The defect this pins: the first implementation emitted per-term
# anchors (`#slab`, `#tlb-shootdown`). GitHub mints anchors for
# HEADINGS only, so a bolded list item has none and every one of those
# links silently resolved to nothing — the reader landed at the top of
# the file with no error anywhere. Found by dry-running phase 4 over
# the corpus and grepping sys/vm/README.md for matching headings.
#
# The invariant, stated so it cannot regress: an anchor the linker
# emits must match a heading that actually exists in the target file.
if os.path.exists(real):
    with open(real, errors="replace") as f:
        rtext = f.read()
    headings = {
        mod._github_anchor(h)
        for h in re.findall(r"^#{1,6}\s+(.*)$", rtext, re.MULTILINE)
    }
    out, _ = mod._link_glossary_first_use(rtext, "sys/vm/README.md", {})
    anchors = set(re.findall(r"\]\(#([a-z0-9-]+)\)", out))
    check("linker emitted at least one anchor", bool(anchors),
          f"anchors={anchors}")
    bad = sorted(a for a in anchors if a not in headings)
    check("no emitted anchor is a dead target", not bad,
          f"dead anchors: {bad}" if bad else f"all of {sorted(anchors)} "
          f"match a heading")
else:
    # A committed fixture is missing -- that is a repo defect, not an
    # environment difference, so fail instead of skipping quietly.
    check("fixture vm_README.snapshot.md present", False, f"missing: {real}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("all glossary-linker tests passed")
