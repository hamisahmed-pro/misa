#!/usr/bin/env node
"use strict";
const n = Number.parseInt(process.argv[2] || "500000", 10);
if (!Number.isSafeInteger(n) || n < 0) throw new Error("n must be a non-negative safe integer");
if (n < 2) {
  console.log("0 0");
} else {
  const prime = new Uint8Array(n + 1);
  prime.fill(1);
  prime[0] = 0; prime[1] = 0;
  for (let p = 2; p * p <= n; p++) {
    if (prime[p]) for (let j = p * p; j <= n; j += p) prime[j] = 0;
  }
  let count = 0, total = 0;
  for (let i = 2; i <= n; i++) if (prime[i]) { count++; total += i; }
  console.log(count + " " + total);
}
