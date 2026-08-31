#!/usr/bin/env python3
"""resolve_c_definition must find declaration-only symbols.

`_extract_func_sigs` originally required a trailing `{`, so it matched
function *definitions* only. Three real, citable classes have no body
anywhere in the tree and were therefore unresolvable:

  1. prototypes            `void cc_conn_init(struct tcpcb *tp);`
  2. fnptr struct members   `int (*sv_fetch_syscall_args)(...);`
  3. kobj interface methods `METHOD uint32_t getptr { ... }` in a .m file

The writer could not confirm any of them, and the reviewer flagged
correct prose as hallucinated -- observed on ch37 (four cc_* KPIs) and
ch39 (`getptr`) during the 2026-08-31 regen.

The negative half matters as much: `cc_record_rtt`, `cc_rttsample` and
`cc_newround` do NOT exist in sys/. The original "Could not find
definition" was correct for those, and a looser matcher that resolves
them would be a regression, not a fix.
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

SRC = os.path.expanduser("~/freebsd-src")
HAVE_SRC = os.path.isdir(os.path.join(SRC, "sys"))
failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")
    if not ok:
        failures.append(label)


def sig_names(rel):
    """Symbol names _extract_func_sigs pulls out of a real source file."""
    path = os.path.join(SRC, rel)
    content = open(path, errors="ignore").read()
    return {s.split(" ", 1)[0] for s in mod._extract_func_sigs(content, rel)}


print("\n1) prototypes (declaration, no body) resolve")
if not HAVE_SRC:
    print("  [SKIP] no ~/freebsd-src")
else:
    names = sig_names("sys/netinet/tcp_var.h")
    for sym in ("cc_conn_init", "cc_post_recovery", "cc_after_idle",
                "cc_ecnpkt_handler"):
        check(f"{sym} found in tcp_var.h", sym in names)

print("\n2) function-pointer struct members resolve")
if not HAVE_SRC:
    print("  [SKIP] no ~/freebsd-src")
else:
    names = sig_names("sys/vm/vm_pager.h")
    for sym in ("pgo_getpages", "pgo_putpages"):
        check(f"{sym} found in vm_pager.h", sym in names)

print("\n3) kobj interface methods resolve from .m files")
if not HAVE_SRC:
    print("  [SKIP] no ~/freebsd-src")
else:
    rel = "sys/dev/sound/pcm/channel_if.m"
    content = open(os.path.join(SRC, rel), errors="ignore").read()
    # getattr, not a direct call: if the helper is missing this must be
    # one named failure, not an AttributeError that kills groups 4-7.
    _kobj = getattr(mod, "_extract_kobj_methods", None)
    check("_extract_kobj_methods exists", _kobj is not None)
    kn = {k.split(" ", 1)[0] for k in _kobj(content, rel)} if _kobj else set()
    for sym in ("getptr", "getcaps", "notify", "trigger", "setformat"):
        check(f"kobj method {sym} extracted", sym in kn)
    check("kobj extractor labels its provenance",
          bool(_kobj) and any("kobj interface method" in k
                              for k in _kobj(content, rel)))

print("\n4) NEGATIVE: nonexistent symbols stay unresolved")
if not HAVE_SRC:
    print("  [SKIP] no ~/freebsd-src")
else:
    seen = set()
    for rel in ("sys/netinet/tcp_var.h", "sys/netinet/cc/cc.h"):
        if os.path.exists(os.path.join(SRC, rel)):
            seen |= sig_names(rel)
    for sym in ("cc_record_rtt", "cc_rttsample", "cc_newround"):
        check(f"{sym} correctly NOT found", sym not in seen,
              "these are writer hallucinations; resolving them "
              "would be a regression")

print("\n5) statements are not mistaken for declarations")
if not HAVE_SRC:
    print("  [SKIP] no ~/freebsd-src")
else:
    # `return f(a, b);` parses as return-type `return` declaring `f`
    # unless statement keywords are excluded; `x = f(a);` likewise.
    for rel in ("sys/dev/sound/pcm/channel.c", "sys/kern/kern_synch.c",
                "sys/vm/vm_page.c", "sys/kern/vfs_syscalls.c"):
        path = os.path.join(SRC, rel)
        if not os.path.exists(path):
            continue
        content = open(path, errors="ignore").read()
        _decl = getattr(mod, "_FUNC_DECL_RE", None)
        if _decl is None:
            check("_FUNC_DECL_RE exists", False)
            break
        bad = [m.group(0).strip()
               for m in _decl.finditer(content)
               if "=" in m.group(0)
               or m.group(0).strip().startswith(
                   ("return", "if", "else", "while", "for", "goto"))]
        check(f"no statement misparsed in {os.path.basename(rel)}",
              not bad, (bad[0][:70] if bad else ""))

print("\n6) synthetic shapes")
SYNTH = """
void\tcc_conn_init(struct tcpcb *tp);
int (*sv_fetch_syscall_args)(struct thread *);
pgo_getpages_t\t\t*pgo_getpages;
static int real_definition(int a)
{
\treturn helper_call(a, 0);
}
"""
names = {s.split(" ", 1)[0]
         for s in mod._extract_func_sigs(SYNTH, "synth.h")}
check("prototype with tab separator", "cc_conn_init" in names)
check("inline fnptr member", "sv_fetch_syscall_args" in names)
check("typedef'd fnptr member (literal tab)", "pgo_getpages" in names)
check("ordinary definition still found", "real_definition" in names)
check("call inside a body is not a declaration",
      "helper_call" not in names, f"got: {sorted(names)}")

print("\n7) keyword stopwords never leak")
_stop = getattr(mod, "_C_KEYWORD_STOPWORDS", frozenset())
check("_C_KEYWORD_STOPWORDS covers statement keywords",
      {"if", "else", "while", "for", "return",
       "struct", "typedef"} <= _stop)
if HAVE_SRC:
    leaked = sig_names("sys/netinet/tcp_var.h") & _stop
    check("no stopword in real extraction output", not leaked,
          f"leaked: {leaked}" if leaked else "")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")
