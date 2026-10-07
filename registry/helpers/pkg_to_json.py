#!/usr/bin/env python3
"""pkg_to_json.py — convert a package's misa.pkg (TOML) to the manifest
JSON object the registry's POST /v1/publish endpoint accepts.

Normalizations (the server's publish contract):
- [dependencies] / [dev-dependencies] TOML tables become lists of
  {"name": ..., "range": ...} under "dependencies" / "dev_dependencies".

Usage:
    python3 pkg_to_json.py <pkgdir>
Prints the manifest JSON on stdout.
"""
import json
import os
import sys
import tomllib


def dep_list(table):
    out = []
    if isinstance(table, dict):
        for name in sorted(table):
            out.append({"name": name, "range": str(table[name])})
    return out


def main():
    if len(sys.argv) != 2:
        sys.stderr.write("usage: pkg_to_json.py <pkgdir>\n")
        sys.exit(2)
    path = os.path.join(sys.argv[1], "misa.pkg")
    try:
        with open(path, "rb") as f:
            doc = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        sys.stderr.write(f"pkg_to_json: cannot read {path}: {e}\n")
        sys.exit(1)
    if "dependencies" in doc:
        doc["dependencies"] = dep_list(doc["dependencies"])
    if "dev-dependencies" in doc:
        doc["dev_dependencies"] = dep_list(doc.pop("dev-dependencies"))
    print(json.dumps(doc))


main()
