#!/usr/bin/env python3
"""Smoke test for the docs.freebsd.org URL sanitizer.

Run on bigone: `python3 test_doc_url_sanitizer.py`.
Exits non-zero on any test failure.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "gendoc", os.path.join(HERE, "generate-doc.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []


def check(label, cond, info=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if info:
        print(f"         {info}")
    if not cond:
        failures.append(label)


# Force a known-slug index so tests don't depend on the user's
# freebsd-doc state. We pretend handbook has only `geom`, `cutting-edge`,
# `kernelconfig`, `firewalls` — and articles is empty.
mod._KNOWN_DOC_SLUGS_CACHE = {
    "books/handbook": {"geom", "cutting-edge", "kernelconfig", "firewalls"},
    "articles": set(),
}

# 1) The exact bug from the user's report.
print("Test 1: hallucinated /handbook/geom-class/ list item is dropped")
content = """\
# GEOM

## See Also
- **FreeBSD Handbook**: [Writing a GEOM Class](https://docs.freebsd.org/en/books/handbook/geom-class/)
- **Real link**: [GEOM chapter](https://docs.freebsd.org/en/books/handbook/geom/)
"""
out, dropped = mod._sanitize_doc_urls(content, "sys/geom/README.md")
check("dropped exactly the bogus list item", dropped == 1,
      f"dropped={dropped}")
check("the geom-class line is gone",
      "geom-class" not in out, info=out)
check("the real geom/ link is preserved",
      "handbook/geom/" in out and "GEOM chapter" in out)

# 2) Bare relative form ("books/arch-handbook/jail/") is treated as
#    bogus — there's no host so markdown can't resolve it.
print("Test 2: bare-relative books/arch-handbook/... list item is dropped")
content2 = """\
## See Also
- [FreeBSD Handbook: Jail Subsystem](books/arch-handbook/jail/) — Jail documentation
- [Real link](https://docs.freebsd.org/en/books/handbook/firewalls/)
"""
out2, dropped2 = mod._sanitize_doc_urls(content2, "sys/net/README_vnet.md")
check("dropped 1 bogus relative link", dropped2 == 1,
      f"dropped={dropped2}")
check("'Jail Subsystem' line is gone",
      "Jail Subsystem" not in out2, info=out2)
check("real firewalls link still there",
      "firewalls" in out2)

# 3) Inline-prose link with bogus slug → strip wrapper, keep label.
print("Test 3: inline-prose bogus link keeps label, drops link")
prose = "See [Writing a GEOM Class](https://docs.freebsd.org/en/books/handbook/geom-class/) for details."
out3, dropped3 = mod._sanitize_doc_urls(prose, "sys/geom/README.md")
check("dropped 1 inline link", dropped3 == 1)
check("label kept, link gone",
      "Writing a GEOM Class" in out3 and "geom-class" not in out3,
      info=out3)
check("surrounding prose preserved",
      out3.startswith("See ") and out3.endswith(" for details."))

# 4) Real handbook URL passes through unchanged.
print("Test 4: real handbook URL unchanged")
real = "- [Kernel Config](https://docs.freebsd.org/en/books/handbook/kernelconfig/)\n"
out4, dropped4 = mod._sanitize_doc_urls(real, "any.md")
check("dropped 0", dropped4 == 0)
check("output identical", out4 == real)

# 5) Idempotent on already-clean output.
print("Test 5: idempotent")
out5, dropped5 = mod._sanitize_doc_urls(out, "sys/geom/README.md")
check("second pass yields no changes",
      out5 == out and dropped5 == 0)

# 6) Non-handbook URLs are left alone (GitHub, RFCs, anything else).
print("Test 6: non-handbook URLs untouched")
mixed = """\
## See Also
- [GitHub](https://github.com/freebsd/freebsd-src)
- [RFC 793](https://www.rfc-editor.org/rfc/rfc793)
- [Anchor](#overview)
- [mailto](mailto:foo@example.com)
"""
out6, dropped6 = mod._sanitize_doc_urls(mixed, "any.md")
check("dropped nothing", dropped6 == 0)
check("output identical", out6 == mixed)

# 7) URL with anchor / trailing path on a real slug → still recognized.
print("Test 7: real slug with anchor / sub-path is preserved")
anchored = (
    "- [Updating](https://docs.freebsd.org/en/books/handbook/cutting-edge/#makeworld)\n"
)
out7, dropped7 = mod._sanitize_doc_urls(anchored, "any.md")
check("real slug + anchor passes through",
      dropped7 == 0 and out7 == anchored)

# 8) http:// (not https://) on a real slug also recognized.
print("Test 8: scheme variation accepted")
http_form = "- [Firewalls](http://docs.freebsd.org/en/books/handbook/firewalls/)\n"
out8, dropped8 = mod._sanitize_doc_urls(http_form, "any.md")
check("http scheme accepted as long as slug is real",
      dropped8 == 0 and out8 == http_form)

# 9) Locale-less URL form (no /en/) still works.
print("Test 9: locale-less URL form")
no_locale = "- [Firewalls](https://docs.freebsd.org/books/handbook/firewalls/)\n"
out9, dropped9 = mod._sanitize_doc_urls(no_locale, "any.md")
check("no-locale form recognized", dropped9 == 0 and out9 == no_locale)

bogus_no_locale = "- [Bogus](https://docs.freebsd.org/books/handbook/geom-class/)\n"
out9b, dropped9b = mod._sanitize_doc_urls(bogus_no_locale, "any.md")
check("no-locale form bogus slug also dropped", dropped9b == 1)

# 10) A `books/handbook/<bogus>/` bare relative is also dropped (covers
#     the exact shape `_DOC_FREEBSD_RELATIVE_RE` is meant to catch).
print("Test 10: bare books/handbook/<bogus>/ list item dropped")
br = "- [Foo](books/handbook/geom-class/)\n"
out10, dropped10 = mod._sanitize_doc_urls(br, "any.md")
check("bare relative bogus dropped", dropped10 == 1, info=out10)

br_real = "- [Real](books/handbook/geom/)\n"
out10b, dropped10b = mod._sanitize_doc_urls(br_real, "any.md")
check("bare relative real slug NOT dropped", dropped10b == 0,
      info=out10b)
# (We don't rewrite this — the URL still has no host, but the slug
# is real, so we trust the writer that they meant a real page. A
# follow-up could prepend the docs.freebsd.org host; out of scope here.)

# 11) End-to-end check on the actual corpus, if present.
real_root = os.path.expanduser("~/freebsd-src")
if os.path.isdir(real_root):
    print(f"Test 11: end-to-end on real corpus at {real_root}")
    # Restore real cache so the on-disk freebsd-doc index is used.
    mod._KNOWN_DOC_SLUGS_CACHE = None
    geom = os.path.join(real_root, "sys/geom/README.md")
    if os.path.isfile(geom):
        text = open(geom).read()
        if "geom-class" in text:
            fixed, drop11 = mod._sanitize_doc_urls(
                text, "sys/geom/README.md"
            )
            check("real sys/geom/README.md: geom-class link dropped",
                  drop11 >= 1 and "geom-class" not in fixed)
        else:
            print("  (skipped — geom-class link already absent from on-disk file)")
    else:
        print(f"  (skipped — {geom} not present)")
else:
    print(f"(skipped Test 11 — {real_root} not present)")

print()
print("=" * 60)
if failures:
    print(f"FAILED: {len(failures)} test(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All tests passed.")
