# Matched six-language benchmark

This directory contains one deliberately small, portable CPU workload: a
Sieve of Eratosthenes that prints the count and sum of primes up to N. It has
implementations in Misa, Python, Node.js, PHP, Go, and Rust.

## Run

Install Python 3.12+, Node.js, PHP CLI, Go, Rust (rustc), a C compiler,
llvmlite 0.50.0, and (on Linux) libgc-dev. From the repository root:

    python bench/multilang/run.py --n 500000 --runs 9 --warmups 2 --json bench/results/multilang.json

The runner compiles Go, Rust, and Misa before timing; validates exact output
against Python before any sample is collected and on every run; randomizes
implementation order for each warmup and measured round; records all samples,
medians, variability, source identity, toolchain versions, LLVM version, and
allocator mode; and exits non-zero on any mismatch or failed process.

For N=500000, the known answer is 41538 9914236195. The CI workflow checks this answer at N=500000 and benchmarks all six implementations at three input scales (N=50000, 200000, and 500000). Each scale has its own raw JSON artifact and validates every sample.

## Interpretation limits

This is an initial **matched-workload smoke benchmark**, not a ranking of
programming languages. It uses one algorithm and one input size; implementation
idioms, garbage collection, runtime startup, and library primitives differ.
The elapsed time intentionally includes process startup but excludes compilation.
Do not extrapolate this result to web services, AI-agent workloads, memory
safety, developer productivity, or overall language performance.

For meaningful claims, add multiple independently implemented workloads,
multiple input scales and host architectures, report uncertainty, and keep
compiler/runtime settings fixed. The canonical Misa-vs-Python suite remains
python impl/bin/misa benchmark; this harness is a separate cross-language
sanity check.


## Profile the Misa AOT hot path

On Linux with `perf` available and permitted by the kernel/container:

    bash bench/profile_aot.sh bench/multilang/sieve.misa bench/results/profile-sieve 500000

The script compiles before profiling and writes `metadata.txt`, `stdout.txt`,
`perf.data`, and a symbol-sorted `hot-symbols.txt`. Review the profile before
changing boxing, dynamic dispatch, allocation, or MIR lowering. In restricted
containers, Linux perf may be unavailable; that is a profiling-environment
limitation, not a benchmark result. Keep profile output out of source control.
