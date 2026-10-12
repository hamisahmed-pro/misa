#!/usr/bin/env python3
"""Sieve of Eratosthenes benchmark reference."""
import sys

def main(n: int) -> None:
    if n < 2:
        print("0 0")
        return
    prime = bytearray(b"\x01") * (n + 1)
    prime[0:2] = b"\x00\x00"
    p = 2
    while p * p <= n:
        if prime[p]:
            start = p * p
            prime[start:n + 1:p] = b"\x00" * (((n - start) // p) + 1)
        p += 1
    values = (i for i in range(2, n + 1) if prime[i])
    count = 0
    total = 0
    for value in values:
        count += 1
        total += value
    print(f"{count} {total}")

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 500000)
