#!/usr/bin/env python3
"""Regression test for retaining native symbols in profiling builds."""
import os
import subprocess
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MISA = os.path.join(ROOT, "impl", "bin", "misa")
SOURCE = os.path.join(ROOT, "bench", "multilang", "sieve.misa")


def run(command):
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                          timeout=180)


def main():
    with tempfile.TemporaryDirectory(prefix="misa-symbols-") as tmp:
        stripped = os.path.join(tmp, "sieve-release")
        release = run([MISA, "build", SOURCE, "-o", stripped])
        if release.returncode:
            raise SystemExit("default release build failed:\n" +
                             release.stdout + release.stderr)
        release_sections = run(["readelf", "-S", stripped])
        if release_sections.returncode or ".symtab" in release_sections.stdout:
            raise SystemExit("default release build unexpectedly retained .symtab")

        binary = os.path.join(tmp, "sieve-profile")
        build = run([MISA, "build", SOURCE, "-o", binary, "--keep-symbols"])
        if build.returncode:
            raise SystemExit("symbol-preserving build failed:\n" +
                             build.stdout + build.stderr)

        nm = run(["nm", "-n", binary])
        if nm.returncode:
            raise SystemExit("nm could not inspect symbol-preserving binary:\n" +
                             nm.stdout + nm.stderr)
        named_text = [
            line for line in nm.stdout.splitlines()
            if len(line.split()) >= 3 and line.split()[-2] in ("T", "t")
        ]
        if not named_text:
            raise SystemExit("symbol-preserving binary has no named text symbols")

        readelf = run(["readelf", "-S", binary])
        if readelf.returncode or ".symtab" not in readelf.stdout:
            raise SystemExit("symbol-preserving binary is missing .symtab:\n" +
                             readelf.stdout + readelf.stderr)

    print("Native symbol-preservation regression test passed.")


if __name__ == "__main__":
    main()
