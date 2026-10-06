"""Misa v0.2 module loader: resolves `import` chains into one merged program.

load(main_path) -> (merged_program, errors, file_order)

- `import "file.misa"` / `./x.misa` / `../x.misa`: relative file (v0.2).
- `import "pkg"` / `import "pkg/sub/mod"`: package import, resolved via
  misa.pkg manifests (see misa/pkg.py): walk-up misa_pkgs/, ~/.misa/pkgs/,
  then the bundled stdlib. Bad manifest -> E113.
- Depth-first, post-order: dependencies' items come before the importer's.
- Duplicate files are included once. Cycles -> E111, missing -> E112.
"""
from __future__ import annotations
import os
from typing import Dict, List, Tuple

from . import ast
from .parser import parse_source, ParseError
from .lexer import LexError
from .pkg import resolve_package, PkgResolveError


def _err(code, message, line=0, col=0, hint=""):
    return {"code": code, "message": message,
            "line": line, "col": col, "hint": hint}


def load(main_path: str):
    main_abs = os.path.abspath(main_path)
    seen: Dict[str, ast.Program] = {}
    order: List[str] = []
    errors: List[dict] = []
    stack: List[str] = []

    def visit(path: str, item=None):
        # L-1: the importing ImportDecl's position is threaded through so
        # E111/E112 point at the offending `import` line, not <unknown>.
        ap = os.path.abspath(path)
        iline = item.line if item is not None else 0
        icol = item.col if item is not None else 0
        if ap in stack:
            cycle = " -> ".join(
                [os.path.basename(p) for p in stack] + [os.path.basename(ap)])
            errors.append(_err("E111", f"circular import: {cycle}",
                               iline, icol,
                               hint="imports must form a DAG"))
            return
        if ap in seen:
            return
        try:
            with open(ap, encoding="utf-8") as f:
                src = f.read()
        except OSError:
            errors.append(_err("E112", f"cannot read import '{path}'",
                               iline, icol,
                               hint="path is relative to the importing file"))
            return
        try:
            prog = parse_source(src, ap)
        except (ParseError, LexError) as e:
            errors.append(_err("E001", str(e), e.line, e.col))
            return
        stack.append(ap)
        for item in prog.items:
            if isinstance(item, ast.ImportDecl):
                rel = item.path
                if rel.endswith(".misa") or rel.startswith(
                        ("./", "../", "/")):
                    # Relative file import (v0.2 behavior).
                    if not rel.endswith(".misa"):
                        errors.append(_err(
                            "E112",
                            f"import path must end in .misa: '{rel}'",
                            item.line, item.col))
                        continue
                    ipath = os.path.normpath(
                        os.path.join(os.path.dirname(ap), rel))
                else:
                    # Package import: `import "pkg"` / `import "pkg/mod"`.
                    try:
                        ipath = resolve_package(
                            rel, os.path.dirname(ap))
                    except PkgResolveError as e:
                        errors.append(_err(
                            e.code, e.message,
                            item.line, item.col, hint=e.hint))
                        continue
                item.resolved = ipath
                visit(ipath, item)
        stack.pop()
        seen[ap] = prog
        order.append(ap)

    if not main_abs.endswith(".misa"):
        errors.append(_err("E112", f"not a .misa file: '{main_path}'"))
        return None, errors, [], {}
    visit(main_abs)

    merged = ast.Program(line=0, col=0)
    file_of: Dict[int, str] = {}
    for ap in order:
        for item in seen[ap].items:
            merged.items.append(item)
            file_of[id(item)] = ap
    return merged, errors, order, file_of
