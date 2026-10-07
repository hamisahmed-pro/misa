#!/usr/bin/env python3
"""decode_tarball.py — base64 -> bytes for the Misa registry server.

Misa values are UTF-8 Text only, so binary tarball bytes never enter
the interpreter. The server writes the client's base64 payload to a
file with fs.write, then this helper decodes it, writes the raw bytes,
and reports the sha256 + size as JSON on stdout.

Usage:
    python3 decode_tarball.py <b64_in> <bin_out>
"""
import base64
import hashlib
import json
import sys


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("usage: decode_tarball.py <b64_in> <bin_out>\n")
        sys.exit(2)
    with open(sys.argv[1], encoding="ascii") as f:
        b64 = f.read().strip()
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        sys.stderr.write(f"decode_tarball: invalid base64: {e}\n")
        sys.exit(1)
    with open(sys.argv[2], "wb") as f:
        f.write(raw)
    print(json.dumps({
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }))


main()
