# Benchmark Coverage Gaps

## Missing Misa Ports (3/10)

### knucleotide
- **Issue**: Map API (`counts.keys()`, `k in counts`) not fully supported
- **Workaround**: None — requires Map iteration API
- **Status**: Blocked on stdlib

### pidigits  
- **Issue**: Requires arbitrary-precision arithmetic (BigInt)
- **Misa status**: No BigInt type
- **Status**: Unsupported — needs BigInt implementation

### regex-redux
- **Issue**: No runtime regex module (only check-time validation)
- **Misa status**: `regexp.py` exists for validation, no `regex.find_all()` runtime
- **Status**: Blocked on stdlib

## Coverage: 7/10 benchmarks
- ✅ n-body, spectral-norm, fannkuch-redux, mandelbrot, fasta, revcomp, binary-trees
- ❌ knucleotide, pidigits, regex-redux
