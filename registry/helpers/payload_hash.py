#!/usr/bin/env python3
"""Compute the v1.1 signed-payload hash for a package tarball.

Usage: payload_hash.py <namespace> <name> <version> <tarball_path>
Prints JSON: {"payload_hash": "<hex sha256>"}

The signed payload is:
  SHA256(namespace + "\\0" + name + "\\0" + version + "\\0" + tarball_bytes)
The client signs the UTF-8 bytes of the hex digest.
"""
import hashlib
import json
import sys


def main() -> int:
    if len(sys.argv) != 5:
        print(json.dumps({"error": "usage: payload_hash.py ns name version tarball"}))
        return 2
    ns, name, version, path = sys.argv[1:5]
    h = hashlib.sha256()
    h.update(ns.encode("utf-8"))
    h.update(b"\x00")
    h.update(name.encode("utf-8"))
    h.update(b"\x00")
    h.update(version.encode("utf-8"))
    h.update(b"\x00")
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    print(json.dumps({"payload_hash": h.hexdigest()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
