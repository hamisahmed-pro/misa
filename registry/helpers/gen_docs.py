#!/usr/bin/env python3
"""Generate static HTML docs for a published package version.

Usage: gen_docs.py <pkgdir> <out_html> <name> <version>
  pkgdir: extracted package directory (has misa.pkg, README.md?, src/)
  out_html: where to write the HTML file

Renders README.md (if present) plus API docs from `///` doc comments
on public fn/type items in src/*.misa. All HTML is escaped — no raw
HTML passthrough from package content.
"""
import html
import json
import os
import re
import sys


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def md_to_html(md: str) -> str:
    """Minimal Markdown: headings, code blocks, inline code, bold,
    italic, links, lists, paragraphs. Everything escaped first."""
    md = esc(md)
    out = []
    in_code = False
    in_list = False
    for line in md.split("\n"):
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(line)
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            if in_list:
                out.append("</ul>")
                in_list = False
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            continue
        if re.match(r"^(\*|-)\s+", line):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(re.sub(r'^(\*|-)\s+', '', line))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.strip() == "":
            continue
        out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _inline(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s


def extract_api(pkgdir: str) -> list:
    """[(kind, name, sig, doc)] from /// comments in src/*.misa."""
    items = []
    srcdir = os.path.join(pkgdir, "src")
    if not os.path.isdir(srcdir):
        return items
    for fn in sorted(os.listdir(srcdir)):
        if not fn.endswith(".misa"):
            continue
        path = os.path.join(srcdir, fn)
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        pending: list[str] = []
        for line in lines:
            s = line.strip()
            if s.startswith("///"):
                pending.append(s[3:].strip())
                continue
            m = re.match(r"^(pub\s+)?fn\s+(\w+)\s*(\([^)]*\))(\s*->\s*([^{\s]+))?",
                         s)
            if m and pending:
                sig = f"fn {m.group(2)}{m.group(3)}"
                if m.group(5):
                    sig += f" -> {m.group(5)}"
                items.append(("fn", m.group(2), sig, "\n".join(pending)))
                pending = []
                continue
            m = re.match(r"^(pub\s+)?type\s+(\w+)", s)
            if m and pending:
                items.append(("type", m.group(2), f"type {m.group(2)}",
                              "\n".join(pending)))
                pending = []
                continue
            if s and not s.startswith("//"):
                pending = []
    return items


def main() -> int:
    if len(sys.argv) != 5:
        print(json.dumps({"error": "usage: gen_docs.py pkgdir out name version"}))
        return 2
    pkgdir, out_html, name, version = sys.argv[1:5]
    readme = ""
    rp = os.path.join(pkgdir, "README.md")
    if os.path.isfile(rp):
        with open(rp, encoding="utf-8", errors="replace") as f:
            readme = f.read()
    api = extract_api(pkgdir)

    parts = [f"<h1>{esc(name)} <small>{esc(version)}</small></h1>"]
    if readme:
        parts.append(md_to_html(readme))
    if api:
        parts.append("<h2>API</h2>")
        for kind, iname, sig, doc in api:
            parts.append(f"<h3><code>{esc(sig)}</code></h3>")
            if doc:
                parts.append(md_to_html(doc))
    if not readme and not api:
        parts.append("<p>No documentation provided for this version.</p>")

    page = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            f"<title>{esc(name)} {esc(version)} — docs</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:760px;"
            "margin:2em auto;padding:0 1em;line-height:1.5}"
            "code{background:#f4f4f4;padding:0.1em 0.3em;border-radius:3px}"
            "pre{background:#f4f4f4;padding:1em;overflow-x:auto}</style>"
            "</head><body>" + "\n".join(parts) + "</body></html>")
    os.makedirs(os.path.dirname(out_html) or ".", exist_ok=True)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(page)
    print(json.dumps({"ok": True, "api_items": len(api),
                      "has_readme": bool(readme)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
