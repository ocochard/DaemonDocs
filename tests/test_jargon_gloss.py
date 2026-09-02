#!/usr/bin/env python3
"""Tests for the undefined-jargon / unexpanded-acronym checker.

Motivating defect (ch12, buffer cache, 2026-08-27): the draft said

    "The `BIO_UNMAPPED` flag indicates that the data pages are not mapped
     into KVM and must be mapped before the I/O can proceed."

`KVM` appeared five times in that chapter and was never defined, and the
text never said why mapped and unmapped modes both exist. Reviewer
criterion 8 ("Rationale") graded the chapter PASS anyway — asking a model
to simulate a junior reader does not work, so the check moved into Python.

What is pinned here:
  1. an undefined curated term is reported;
  2. a term glossed on first use is NOT reported (no nagging);
  3. terms only inside fenced code blocks are NOT reported;
  4. a `## Glossary` definition counts as a gloss;
  5. repeated unexpanded acronyms are caught, expanded ones are not;
  6. the exempt set suppresses common-knowledge acronyms;
  7. the real ch12 text reproduces the original miss;
  8. findings stay OUT of `total_issues` (they are readability, not
     accuracy, and must not silently change the fact-fix loop's trigger).

Run: `python3 test_jargon_gloss.py`. Exits non-zero on any failure.
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


print("1) undefined curated term is reported")
# Bare use, no definitional cue anywhere near it.
d = ("## Deep Dive\n\n"
     "The buffer's pages are not mapped into KVM at this point, so the "
     "driver must arrange for them to become reachable before proceeding "
     "with the transfer. This happens well before the completion callback "
     "runs and has no bearing on the rest of the path.\n")
got = mod._extract_unglossed_jargon(d)
check("KVM flagged when never defined", "KVM" in got, f"got {got}")

print()
print("2) a glossed term is not reported")
for gloss in (
    "KVM (the kernel's virtual address space) is where the buffer lands.",
    "KVM — the kernel virtual address space — holds the mapping.",
    "KVM is a region of address space reserved for the kernel.",
    "KVM stands for kernel virtual memory.",
):
    got = mod._extract_unglossed_jargon(f"## Deep Dive\n\n{gloss}\n")
    check(f"not flagged: {gloss[:42]}...", "KVM" not in got, f"got {got}")

print()
print("3) code-block-only use is not prose use")
d = ("## Key Data Structures\n\n"
     "The relevant flag lives in the I/O request structure.\n\n"
     "```c\n"
     "/* set when pages are not mapped into KVM */\n"
     "#define BIO_UNMAPPED 0x10\n"
     "struct bio { int bio_flags; };\n"
     "```\n")
got = mod._extract_unglossed_jargon(d)
check("KVM inside a fence is not flagged", "KVM" not in got, f"got {got}")

print()
print("4) an explicit Glossary entry counts as a gloss")
d = ("## Glossary\n\n"
     "**KVM** — the kernel's virtual address space.\n\n"
     "## Deep Dive\n\n"
     "The pages are not mapped into KVM, so the driver maps them first "
     "and then issues the transfer to the underlying device.\n")
got = mod._extract_unglossed_jargon(d)
check("Glossary definition suppresses the finding", "KVM" not in got,
      f"got {got}")

print()
print("5) unexpanded vs expanded acronyms")
d = ("## Deep Dive\n\n"
     "The PMAP layer coordinates with the scheduler. Later the PMAP "
     "layer is consulted again during teardown of the address space.\n")
got = mod._extract_unexpanded_acronyms(d)
check("repeated unexpanded acronym is caught", "PMAP" in got, f"got {got}")

d = ("## Deep Dive\n\n"
     "The physical map (PMAP) layer coordinates with the scheduler. "
     "Later the PMAP layer is consulted again during teardown.\n")
got = mod._extract_unexpanded_acronyms(d)
check("expanded acronym is not caught", "PMAP" not in got, f"got {got}")

d = ("## Deep Dive\n\n"
     "The XYZZY subsystem is mentioned exactly once here.\n")
got = mod._extract_unexpanded_acronyms(d)
check("single mention is ignored", "XYZZY" not in got, f"got {got}")

print()
print("6) exempt acronyms stay quiet")
d = ("## Deep Dive\n\n"
     "The CPU issues a TCP segment over IP. The CPU then waits for the "
     "TCP acknowledgement before releasing the IP buffer.\n")
got = mod._extract_unexpanded_acronyms(d)
noisy = [a for a in got if a in {"CPU", "TCP", "IP"}]
check("common-knowledge acronyms suppressed", not noisy, f"got {got}")

d = ("## Deep Dive\n\n"
     "Identifiers such as `BIO_UNMAPPED` and `M_NOWAIT` appear often; "
     "`BIO_UNMAPPED` again, and `M_NOWAIT` once more.\n")
got = mod._extract_unexpanded_acronyms(d)
check("inline-code identifiers are not acronyms",
      not any(a.startswith(("BIO", "M_")) for a in got), f"got {got}")

print()
print("7) the real ch12 sentence reproduces the original miss")
# Frozen copy, not the live artifact. This block pins a DEFECT that was
# present in the shipped chapter (KVM used in prose, no Glossary to
# define it) -- which means the moment ch11 regenerates and the pipeline
# fixes that defect, the test fails and reads like a code regression.
# The snapshot was taken 2026-09-02, immediately before the step-3 A/B
# regenerated this chapter on both arms.
real = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "fixtures", "vm_README_bcache.snapshot.md")
if not os.path.exists(real):
    # A committed fixture is missing -- that is a repo defect, not an
    # environment difference, so fail instead of skipping quietly.
    check("fixture vm_README_bcache.snapshot.md present", False, f"missing: {real}")
else:
    text = open(real, encoding="utf-8").read()
    got = mod._extract_unglossed_jargon(text)
    check("ch12 flags KVM as unglossed", "KVM" in got,
          f"flagged: {got}")
    # Sanity: the term really is used in prose and really is undefined,
    # so the finding is about the document and not an artifact of the scan.
    check("ch12 does use KVM in prose", "KVM" in text)
    check("ch12 has no Glossary section",
          not mod._find_glossary_section(text).strip())

print()
print("8) readability findings stay out of total_issues")
d = ("## Deep Dive\n\n"
     "The pages are not mapped into KVM before the transfer begins, and "
     "the turnstile is handed off to the next waiter in the queue.\n")
facts = mod.fact_check_draft(d, mod.SRC_ROOT)
check("jargon_unglossed key present", "jargon_unglossed" in facts)
check("acronyms_unexpanded key present", "acronyms_unexpanded" in facts)
check("jargon findings non-empty for this draft",
      bool(facts.get("jargon_unglossed")),
      f"got {facts.get('jargon_unglossed')}")
# The fact-fix loop triggers on total_issues; readability must not move it.
recomputed = (
    len(facts['file_paths_not_found']) + len(facts['file_paths_corrected'])
    + len(facts['structs_not_found']) + len(facts['struct_fields_bogus'])
    + len(facts['struct_bodies_abridged'])
    + len(facts['struct_field_refs_bogus']) + len(facts['funcs_not_found'])
    + len(facts['func_sigs_mismatch'])
    + len(facts['kernel_options_not_found'])
    + len(facts['dtrace_probes_not_found'])
    + len(facts['malloc_tags_not_found']) + len(facts['sysctls_not_found'])
)
check("total_issues excludes readability findings",
      facts['total_issues'] == recomputed,
      f"total={facts['total_issues']} accuracy-only={recomputed}")

print()
print("9) reviewer prompt carries the detected terms")
chapter = {"title": "Buffer Cache", "key_questions": ["Why two modes?"],
           "source_files": [], "source_dirs": []}
d = ("## Deep Dive\n\n"
     "The pages are not mapped into KVM before the transfer begins and "
     "the driver must arrange a mapping first.\n")
prompt = mod.build_review_prompt(chapter, d)

# Match the block HEADING, not the bare phrase: criterion 5's static text
# also mentions "Undefined Terms Detected" (it tells the reviewer what to
# do when the block appears), so a substring test is always true.
HEADING = "## Undefined Terms Detected"

check("prompt contains the undefined-terms block", HEADING in prompt)
check("prompt names the offending term", "`KVM`" in prompt)
check("prompt ties it to Accessibility", "Accessibility" in prompt)
check("prompt demands the WHY for two-mode choices",
      "why both exist" in prompt.lower())

# And stays silent on a clean draft, so most chapters see no extra prompt.
clean = ("## Deep Dive\n\n"
         "The subsystem walks a list of requests and dispatches each one "
         "to the provider that claimed it.\n")
check("no block on a clean draft",
      HEADING not in mod.build_review_prompt(chapter, clean))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("all jargon-gloss tests passed")
