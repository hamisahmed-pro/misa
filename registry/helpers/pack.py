#!/usr/bin/env python3
"""pack.py — deterministic tar.gz packing for the Misa registry.

Usage:
    python3 pack.py <pkgdir> <out.tar.gz>

Deterministic: entry names sorted, mtimes zeroed, uid/gid zeroed,
user/group names emptied, gzip compresslevel 9. Directory entries are
included (sorted) so unpacking reproduces the tree exactly.
"""
import os
import sys
import tarfile


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("usage: pack.py <pkgdir> <out.tar.gz>\n")
        sys.exit(2)
    pkgdir, out = sys.argv[1], sys.argv[2]
    entries = []
    for dirpath, dirnames, filenames in os.walk(pkgdir):
        dirnames.sort()
        for d in sorted(dirnames):
            entries.append(os.path.join(dirpath, d))
        for f in sorted(filenames):
            entries.append(os.path.join(dirpath, f))
    # deterministic order by archive name
    entries.sort(key=lambda p: os.path.relpath(p, pkgdir))

    def reset(ti):
        ti.mtime = 0
        ti.uid = 0
        ti.gid = 0
        ti.uname = ""
        ti.gname = ""
        return ti

    with tarfile.open(out, "w:gz", compresslevel=9) as tf:
        for path in entries:
            arcname = os.path.relpath(path, pkgdir)
            tf.add(path, arcname=arcname, recursive=False, filter=reset)
    # include the top-level dir entries' determinism is enough; print size
    sys.stderr.write(f"pack: {out} ({os.path.getsize(out)} bytes)\n")


main()
