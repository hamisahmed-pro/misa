#!/usr/bin/env python3
"""Fetch the SQLite amalgamation (Phase 5, DB).

Downloads sqlite3.c + sqlite3.h (public domain) into
~/workspace/deps/sqlite/. The AOT backend compiles the amalgamation
into the C runtime when a program uses db.* (see
impl/misa/aot/build.py).
"""
import os
import sys
import urllib.request
import zipfile

VERSION = "3500200"  # 3.50.2
URL = f"https://www.sqlite.org/2025/sqlite-amalgamation-{VERSION}.zip"
DEST = os.path.join(os.path.expanduser("~"), "workspace", "deps",
                    "sqlite")


def main():
    os.makedirs(DEST, exist_ok=True)
    have = all(os.path.isfile(os.path.join(DEST, f))
               for f in ("sqlite3.c", "sqlite3.h"))
    if have:
        print(f"sqlite already vendored at {DEST}")
        return
    tmp = os.path.join(DEST, "amalgamation.zip")
    print(f"downloading {URL} ...")
    urllib.request.urlretrieve(URL, tmp)
    with zipfile.ZipFile(tmp) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if base in ("sqlite3.c", "sqlite3.h"):
                with z.open(name) as src, \
                        open(os.path.join(DEST, base), "wb") as dst:
                    dst.write(src.read())
    os.remove(tmp)
    print(f"vendored sqlite3.c/sqlite3.h at {DEST}")


if __name__ == "__main__":
    sys.exit(main())
