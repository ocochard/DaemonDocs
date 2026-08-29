#!/usr/bin/env python3
"""Tests for the gated criterion-8 rationale rubric (step 3).

Criterion 8 asks whether the draft explains WHY a mechanism exists. Two
weaknesses motivated step 3: its examples were all data structures, so
flags and modes never registered as "non-obvious mechanisms"; and it was
one binary verdict over a ~25 KB draft, which invites the reviewer to
stop looking once it has something to say. The motivating defect is ch12
(buffer cache), graded PASS on criterion 8 while using `BIO_UNMAPPED`
five times without ever saying why mapped and unmapped modes both exist.

The change is a PROMPT edit, and `FUTURE_IMPROVEMENTS.md` is largely a
record of prompt edits that broke something else. It therefore ships
behind `DAEMONDOCS_RATIONALE_ENUM`, default OFF, pending an A/B.

Two things make this worth pinning rather than eyeballing:

  * The control arm MUST stay inert. Chapters 33-40 are queued against
    today's rubric, and `build_review_prompt` is called once per review
    round — so a leak would change the rubric between round 1 and round 2
    of a chapter already in flight.
  * The enumeration MUST stay a top-level sibling of `criteria`, never a
    member of it. `_review_passes` rejects any non-string criteria value,
    so the spec's literal `"rationale": {"missing": [...]}` would fail the
    gate for EVERY chapter, exhaust max_revisions and ship everything
    UNVERIFIED. Group 7 is that documented negative.

What is pinned here:
  1. the env idiom, including strict-"1";
  2. control arm inert (the regression test protecting the live queue);
  3. treatment arm adds both halves;
  4. the arm is never half-applied;
  5. the JSON block stays brace-balanced and parseable in both arms;
  6. `_review_passes` / `_criteria_fail_count` tolerate the new key;
  7. the spec's broken shape is rejected (documented negative);
  8. `_N_CRITERIA` is unchanged.

Groups 6-8 are net-new coverage: nothing else tests either consumer.

Run: `python3 test_rationale_rubric.py`. Exits non-zero on any failure.
"""
import importlib.util
import json
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


CHAPTER = {
    "id": 12, "title": "The Buffer Cache", "slug": "buf",
    "paths": ["sys/kern/vfs_bio.c"], "output": "sys/kern/README_bio.md",
    "topics": ["buffer cache", "bio"],
}
DRAFT = "# The Buffer Cache\n\n## Quick Summary\n\nNothing notable here.\n"


def prompt_for(arm):
    """Build a review prompt with RATIONALE_ENUM forced to `arm`.

    The flag is read once at module import, so the arm is varied by
    reassigning the module attribute rather than by setting os.environ.
    That also pins the requirement that `build_review_prompt` reads the
    global at CALL time — if someone captures it into a default argument
    the A/B silently stops working.
    """
    saved = mod.RATIONALE_ENUM
    try:
        mod.RATIONALE_ENUM = arm
        return mod.build_review_prompt(CHAPTER, DRAFT)
    finally:
        mod.RATIONALE_ENUM = saved


def json_block(prompt):
    """Extract the output-schema JSON template from a review prompt."""
    blk = prompt.split("Output a JSON object with this structure — nothing else:")[1]
    return blk.split("Grading rule")[0].strip()


print("1) the env idiom")
# `== "1"`, not `!= "0"`: with a "0" default, an empty export
# (DAEMONDOCS_RATIONALE_ENUM=) would read as ON under the other idiom.
for raw, want in (("1", True), ("0", False), ("", False),
                  ("true", False), (None, False)):
    got = (raw if raw is not None else "0") == "1"
    check(f"{raw!r} -> {want}", got == want)
check("flag exists and is a bool",
      isinstance(mod.RATIONALE_ENUM, bool), f"got {mod.RATIONALE_ENUM!r}")
check("default is OFF when the env var is unset",
      os.environ.get("DAEMONDOCS_RATIONALE_ENUM") is not None
      or mod.RATIONALE_ENUM is False,
      f"RATIONALE_ENUM={mod.RATIONALE_ENUM}")

print()
print("2) control arm is inert (protects the live queue)")
off = prompt_for(False)
check("no rationale_missing key", "rationale_missing" not in off)
check("no BIO_UNMAPPED in the rubric", "BIO_UNMAPPED" not in off)
check("no LK_EXCLUSIVE in the rubric", "LK_EXCLUSIVE" not in off)
check("no 'do not stop at three' rule", "do not stop at three" not in off)
check("criterion 8 still present and unwidened",
      "8. **Rationale**" in off and "FLAGS and MODES" not in off)

print()
print("3) treatment arm adds both halves")
on = prompt_for(True)
check("rationale_missing appears in the schema", "rationale_missing" in on)
check("criterion widened to flags and modes", "FLAGS and MODES" in on)
for term in ("BIO_UNMAPPED", "M_NOWAIT", "M_WAITOK",
             "LK_EXCLUSIVE", "LK_SHARED"):
    check(f"names {term}", term in on)
check("enumeration rule present", "do not stop at three" in on)
check("ties the list to the verdict",
      'If the list is non-empty' in on and '`rationale` MUST start with "FAIL"' in on)
# The widened clause must land INSIDE criterion 8, before its PASS
# condition, so the existing FAIL clause and the trivial-structures
# carve-out below both inherit it instead of applying to structures only.
c8 = on.split("8. **Rationale**")[1].split("## Draft to Review")[0]
check("widened clause is inside criterion 8", "FLAGS and MODES" in c8)
check("widened clause precedes the PASS condition",
      c8.index("FLAGS and MODES") < c8.index("PASS if every non-obvious"))
check("trivial-structures carve-out still applies after it",
      c8.index("FLAGS and MODES") < c8.index("do NOT need"))

print()
print("4) the arm is never half-applied")
# Asking for the enumeration without widening the criterion, or widening
# it with nowhere to record findings, would each measure the wrong thing.
for label, text in (("control", off), ("treatment", on)):
    has_crit = "FLAGS and MODES" in text
    has_json = "rationale_missing" in text
    check(f"{label}: both halves agree", has_crit == has_json,
          f"criterion={has_crit} json={has_json}")

print()
print("5) the JSON block stays valid in both arms")
# Most likely authoring bug in a doubled-brace f-string run through
# textwrap.dedent is an unbalanced brace, which would corrupt the schema
# the reviewer is told to emit.
for label, text in (("control", off), ("treatment", on)):
    blk = json_block(text)
    check(f"{label}: braces balance",
          blk.count("{") == blk.count("}"),
          f"{blk.count('{')} open vs {blk.count('}')} close")
    obj = None
    try:
        obj = json.loads(re.sub(r'"PASS" or "NEEDS_REVISION"', '"PASS"', blk))
    except Exception as e:  # noqa: BLE001
        check(f"{label}: template parses as JSON", False, str(e))
    if obj is not None:
        check(f"{label}: template parses as JSON", True)
        check(f"{label}: criteria values are all strings",
              all(isinstance(v, str) for v in obj["criteria"].values()),
              "a non-string here fails _review_passes for every chapter")
        check(f"{label}: rationale stays a string verdict",
              isinstance(obj["criteria"]["rationale"], str))
        if label == "treatment":
            check("treatment: rationale_missing is TOP-LEVEL",
                  isinstance(obj.get("rationale_missing"), list))
            check("treatment: rationale_missing is NOT inside criteria",
                  "rationale_missing" not in obj["criteria"])
        else:
            check("control: no rationale_missing key at all",
                  "rationale_missing" not in obj)

print()
print("6) both consumers tolerate the new top-level key")


def review(**over):
    r = {
        "grade": "PASS",
        "criteria": {
            "completeness": "PASS: all sections present",
            "accuracy": "PASS: verified",
            "source_coverage": "PASS: files cited",
            "mermaid_diagram": "PASS: not required",
            "accessibility": "PASS: terms glossed",
            "structure": "PASS: ordered",
            "no_marketing": "PASS: none found",
            "rationale": "PASS: rationale given",
        },
        "issues": [], "praise": [],
    }
    r.update(over)
    return r


ok = review(rationale_missing=["BIO_UNMAPPED", "LK_SHARED"])
check("_review_passes approves despite a populated list",
      mod._review_passes(ok) is True)
check("_criteria_fail_count ignores the new key",
      mod._criteria_fail_count(ok["criteria"]) == 0)
check("empty list is equally fine",
      mod._review_passes(review(rationale_missing=[])) is True)
check("absent key (control arm) still approves",
      mod._review_passes(review()) is True)
# A FAIL verdict must still block, list or no list.
fail = review(rationale_missing=["x"])
fail["criteria"]["rationale"] = "FAIL: no rationale for BIO_UNMAPPED"
check("a FAIL rationale verdict still blocks approval",
      mod._review_passes(fail) is False)
check("and still counts as exactly one failing criterion",
      mod._criteria_fail_count(fail["criteria"]) == 1)

print()
print("7) the spec's literal shape is rejected (documented negative)")
# The step-3 spec wrote `"rationale": {"missing": [...]}` INSIDE criteria.
# `_review_passes` returns False on any non-string criteria value, so that
# shape would fail the gate for every chapter, exhaust max_revisions and
# ship the whole corpus UNVERIFIED. This test exists so nobody "fixes" the
# design back to the spec's wording.
broken = review()
broken["criteria"]["rationale"] = {"missing": ["BIO_UNMAPPED"]}
check("dict inside criteria fails the gate",
      mod._review_passes(broken) is False)
check("and is counted as a failing criterion",
      mod._criteria_fail_count(broken["criteria"]) == 1)

print()
print("8) criterion count is unchanged")
# The round print at the review site renders (_N_CRITERIA - fails)/_N_CRITERIA.
check("_N_CRITERIA is still 8", mod._N_CRITERIA == 8,
      f"got {mod._N_CRITERIA}")
check("rationale_missing did not become a graded criterion",
      "rationale_missing" not in mod._REVIEW_CRITERIA)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("all rationale-rubric tests passed")
