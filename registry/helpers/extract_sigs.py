#!/usr/bin/env python3
"""extract_sigs.py — signature extraction for the Misa package registry.

Usage:
    python3 extract_sigs.py <pkgdir> <throws_json> [--misa-root DIR]

Parses every .misa file under <pkgdir>/src, extracts top-level `fn`
declarations, `interface` declarations and `impl` blocks, renders their
types back to canonical Misa type syntax, and merges the checker's
throws graph (<throws_json> = output of `misa check --json <lib>`).

Also collects namespaced builtin calls per function body
(e.g. "http.get", "fs.read", "process.run", "db.exec", "model.run")
so the registry can *infer* capabilities instead of trusting the
manifest's declaration.

Output: JSON document on stdout:
    {"lib": "src/lib.misa",
     "signatures": [ {kind,name,params,ret,throws_declared,throws_inferred,
                      calls,doc,file,line}, ... ]}

Doc comments: the Misa parser does not attach `///` doc comments to
declarations, so `doc` is always "" (recorded for schema completeness).
"""
import json
import os
import sys


def fail(msg):
    sys.stderr.write(f"extract_sigs: error: {msg}\n")
    sys.exit(1)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    misa_root = os.environ.get("MISA_ROOT", ".")
    for a in sys.argv[1:]:
        if a.startswith("--misa-root="):
            misa_root = a.split("=", 1)[1]
    if len(args) < 1:
        fail("usage: extract_sigs.py <pkgdir> [throws_json]")
    pkgdir = args[0]
    throws_path = args[1] if len(args) > 1 else None

    sys.path.insert(0, os.path.join(os.path.abspath(misa_root), "impl"))
    try:
        from misa import ast as A
        from misa.parser import parse_source, ParseError
        from misa.lexer import LexError
    except ImportError as e:
        fail(f"cannot import misa parser from {misa_root}/impl: {e}")

    throws_graph = {}
    if throws_path and os.path.isfile(throws_path):
        try:
            with open(throws_path, encoding="utf-8") as f:
                throws_graph = json.load(f).get("throws_graph", {}) or {}
        except (OSError, ValueError):
            pass

    srcdir = os.path.join(pkgdir, "src")
    files = []
    if os.path.isdir(srcdir):
        for dirpath, _dirnames, filenames in os.walk(srcdir):
            for fn in sorted(filenames):
                if fn.endswith(".misa"):
                    files.append(os.path.join(dirpath, fn))
    files.sort()

    sigs = []
    seen_fn = set()

    for path in files:
        rel = os.path.relpath(path, pkgdir).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8") as f:
                src = f.read()
        except OSError:
            continue
        try:
            prog = parse_source(src, path)
        except (ParseError, LexError) as e:
            fail(f"parse error in {rel}: {e}")
        for item in prog.items:
            if isinstance(item, A.FnDecl):
                if item.name in seen_fn:
                    continue
                seen_fn.add(item.name)
                tg = throws_graph.get(item.name, {})
                sigs.append({
                    "kind": "fn",
                    "name": item.name,
                    # params as [name, type] pairs: Misa cannot name a
                    # field `.type` (keyword) nor do dynamic o[k] lookup,
                    # but list indexing p[0]/p[1] works everywhere.
                    "params": [[n, render_type(t) if t else "any"]
                               for n, t in item.params],
                    "ret": render_type(item.ret) if item.ret else "Void",
                    "throws_declared": sorted(item.throws or []),
                    "throws_inferred": sorted(tg.get("inferred", [])),
                    "calls": sorted(collect_calls(item)),
                    "doc": "",
                    "file": rel,
                    "line": item.line,
                })
            elif isinstance(item, A.InterfaceDecl):
                sigs.append({
                    "kind": "interface",
                    "name": item.name,
                    "params": [{
                        "name": m.name,
                        # rendered signature string (Misa can't spell
                        # a field named `.type`; `sig` is safe)
                        "sig": ("(" + ", ".join(
                            render_type(t) if t else "any"
                            for _, t in m.params) + ") -> " +
                            (render_type(m.ret) if m.ret else "Void")),
                        "throws": sorted(m.throws or []),
                    } for m in item.methods],
                    "ret": "",
                    "throws_declared": [],
                    "throws_inferred": [],
                    "calls": [],
                    "doc": "",
                    "file": rel,
                    "line": item.line,
                })
            elif isinstance(item, A.ImplDecl):
                impl_calls = set()
                for m in item.methods:
                    impl_calls |= collect_calls(m)
                sigs.append({
                    "kind": "impl",
                    "name": item.iface,
                    "params": [{
                        "name": m.name,
                        "sig": ("(" + ", ".join(
                            render_type(t) if t else "any"
                            for _, t in m.params) + ") -> " +
                            (render_type(m.ret) if m.ret else "Void")),
                    } for m in item.methods],
                    "ret": item.target,
                    "throws_declared": [],
                    "throws_inferred": [],
                    "calls": sorted(impl_calls),
                    "doc": f"for {item.target}",
                    "file": rel,
                    "line": item.line,
                })

    lib = "src/lib.misa"
    print(json.dumps({"lib": lib, "signatures": sigs}))


def render_type(t):
    """Render a TypeExpr back to canonical Misa type syntax."""
    if t is None:
        return "any"
    if t.is_func:
        ps = ", ".join(render_type(p) for p in t.func_params)
        r = f"({ps}) -> {render_type(t.func_ret) if t.func_ret else 'Void'}"
        return f"{r}?" if t.optional else r
    if t.is_map:
        k = render_type(t.map_key) if t.map_key else "Text"
        v = render_type(t.map_val) if t.map_val else "any"
        r = f"Map<{k}, {v}>"
        return f"{r}?" if t.optional else r
    if t.is_list:
        inner = render_type(t.list_elem) if t.list_elem is not None \
            else (t.name or "any")
        r = f"[{inner}]"
        return f"{r}?" if t.optional else r
    if t.type_args:
        inner = ", ".join(render_type(a) for a in t.type_args)
        r = f"{t.name}<{inner}>"
        return f"{r}?" if t.optional else r
    r = t.name or "any"
    return f"{r}?" if t.optional else r


def collect_calls(fn):
    """Collect dotted builtin call names (e.g. 'http.get') in a fn body."""
    from misa import ast as A
    found = set()

    def walk(n):
        if isinstance(n, A.Call):
            func = getattr(n, "func", None) or getattr(n, "name", "")
            if isinstance(func, str) and "." in func:
                head = func.split(".")[0]
                if head in ("fs", "http", "net", "process", "db", "model",
                            "crypto", "socket"):
                    found.add(func)
        for _fname, fdef in getattr(n, "__dataclass_fields__", {}).items():
            v = getattr(n, _fname)
            _walk_val(v)

    def _walk_val(v):
        from misa import ast as A2
        if isinstance(v, A2.Node):
            walk(v)
        elif isinstance(v, (list, tuple)):
            for i in v:
                _walk_val(i)

    for stmt in fn.body:
        _walk_val(stmt)
    return found


main()
