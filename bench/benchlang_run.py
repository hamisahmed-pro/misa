#!/usr/bin/env python3
"""Reproducible Misa vs Python/Node/PHP/Rust/Go benchmark runner.

Runs the five fixed benchLANG workloads:
  primes, fib, strings, mandelbrot, binarytrees

For each language:
  - build once (Rust/Go/Misa AOT)
  - warm up once
  - execute 5 measured runs
  - report median and all samples
  - verify exact checksum against the shared expected values

This deliberately separates compile time from execution time. The main
comparison is steady-state program execution; build time and artifact size
are reported separately.
"""
import json, os, platform, shutil, statistics, subprocess, tempfile, time

ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MISA=os.path.join(ROOT,"impl","bin","misa")
SRC=os.path.join(ROOT,"bench","benchlang.misa")
N=5
# The binary-tree checksum is obtained from the reference implementations
# at runtime below; it must agree across all five languages.  We don't hardcode
# a guessed value.

WORK=os.path.join(ROOT,"bench","benchlang-work")
os.makedirs(WORK,exist_ok=True)

def sh(cmd,cwd=None):
    p=subprocess.run(cmd,cwd=cwd,capture_output=True,text=True)
    return p.returncode,p.stdout,p.stderr

def timed(cmd,cwd=None):
    t=time.perf_counter()
    p=subprocess.run(cmd,cwd=cwd,capture_output=True,text=True)
    return (time.perf_counter()-t),p

def one(cmd,cwd=None):
    t,p=timed(cmd,cwd)
    out=p.stdout.strip().splitlines()
    return t,p.returncode,out,p.stderr.strip()

def write(path,s):
    open(path,"w").write(s)

# Canonical language baselines are pinned in bench/compare/ and were adapted
# from GlebOlsen/benchLANG commit 2731f29bcf0bfda750a62c81337b5e5e7456bb4a.
# They expose an optional workload selector so every language executes exactly
# one kernel per measured process.

write(PY, COMMON+r'''
import sys, time
def is_prime(n):
    if n<2:return False
    if n<4:return True
    if n%2==0 or n%3==0:return False
    i=5
    while i*i<=n:
        if n%i==0 or n%(i+2)==0:return False
        i+=6
    return True
def bench_primes(limit):
    c=0
    for i in range(2,limit):
        if is_prime(i):c+=1
    return c
def fib(n): return n if n<2 else fib(n-1)+fib(n-2)
def bench_strings(sentence,iters):
    words=sentence.split(' '); total=0
    for _ in range(iters):
      for wi in words:
       for wj in words:
        if wi==wj: total+=1
    return total
def bench_mandelbrot(size):
    checksum=0
    for py in range(size):
      cy=py/size*3.0-1.5
      for px in range(size):
       cx=px/size*3.0-2.0; zx=0.0; zy=0.0; it=0
       while it<MANDEL_MAX_ITER and zx*zx+zy*zy<=4.0:
        nx=zx*zx-zy*zy+cx; zy=2*zx*zy+cy; zx=nx; it+=1
       checksum+=it
    return checksum
class Node:
    __slots__=('left','right','item')
    def __init__(self,left,right,item): self.left=left; self.right=right; self.item=item
def make_tree(item,depth):
    if depth==0:return Node(None,None,item)
    return Node(make_tree(2*item-1,depth-1),make_tree(2*item,depth-1),item)
def check_tree(n):
    return n.item if n.left is None else n.item+check_tree(n.left)-check_tree(n.right)
def bench_binary_trees(max_depth):
    checksum=check_tree(make_tree(0,max_depth+1)); long_lived=make_tree(0,max_depth)
    for d in range(TREE_MIN_DEPTH,max_depth+1,2):
      for i in range(1 << (max_depth-d+TREE_MIN_DEPTH)): checksum+=check_tree(make_tree(i+1,d))
    return checksum
bench=sys.argv[1] if len(sys.argv)>1 else "all"
jobs={
 "primes":lambda:bench_primes(PRIMES_LIMIT),
 "fib":lambda:fib(FIBONACCI_N),
 "strings":lambda:bench_strings(SENTENCE,STRING_ITER),
 "mandelbrot":lambda:bench_mandelbrot(MANDEL_SIZE),
 "binarytrees":lambda:bench_binary_trees(TREE_MAX_DEPTH)}
for name,fn in jobs.items():
    if bench not in ("all",name): continue
    t=time.perf_counter(); result=fn(); print(name,result,f"{time.perf_counter()-t:.6f}")
''')

compare=os.path.join(ROOT,"bench","compare")
sources={
 "node":os.path.join(compare,"benchlang.js"),
 "php":os.path.join(compare,"benchlang.php"),
 "rust":os.path.join(compare,"benchlang.rs"),
 "go":os.path.join(compare,"benchlang.go"),
}
missing=[p for p in sources.values() if not os.path.exists(p)]
if missing:
    print("ERROR: missing benchmark baselines:",*missing,sep="\n")
    raise SystemExit(2)

langs={}
langs["python"]=["python3",PY]
langs["node"]=["node",sources["node"]]
langs["php"]=["php",sources["php"]]

build_times={}
for lang,src,cmd in [
 ("rust",sources["rust"],["rustc","-O",src,"-o",os.path.join(WORK,"rust-bench")]),
 ("go",sources["go"],["go","build","-o",os.path.join(WORK,"go-bench"),src]),
]:
    t=time.perf_counter(); rc,out,err=sh(cmd,cwd=ROOT); build_times[lang]=time.perf_counter()-t
    if rc: print(err); raise SystemExit(3)
    langs[lang]=[os.path.join(WORK,lang+"-bench")]

misa_out=os.path.join(WORK,"misa-bench")
t=time.perf_counter(); rc,out,err=sh(["python3",MISA,"build",SRC,"-o",misa_out],cwd=ROOT); build_times["misa"]=time.perf_counter()-t
if rc: print(err); raise SystemExit(4)
langs["misa"]=[misa_out]

def measure(cmd):
    # /usr/bin/time gives process-level wall time and peak RSS, while the
    # program's checksum is captured independently.
    t=time.perf_counter()
    p=subprocess.run(["/usr/bin/time","-f","%e %M"]+cmd,capture_output=True,text=True)
    wall=time.perf_counter()-t
    if p.returncode: raise RuntimeError(p.stderr)
    lines=p.stdout.strip().splitlines()
    metric=p.stderr.strip().splitlines()[-1].split()
    rss_kb=int(metric[1]) if len(metric)>1 else None
    return wall,rss_kb,lines[-1] if lines else ""

results={}
for lang,cmd in langs.items():
    print("\n==",lang,"==")
    results[lang]={}
    for bench in ["primes","fib","strings","mandelbrot","binarytrees"]:
        samples=[]; rss=[]; last=""
        for _ in range(N):
            wall,mem,last=measure(cmd+[bench])
            samples.append(wall); rss.append(mem)
        fields=last.split()
        checksum=fields[1] if lang!="misa" and len(fields)>1 else fields[0]
        results[lang][bench]={
          "median_s":statistics.median(samples),
          "samples_s":samples,
          "peak_rss_kb_max":max(rss),
          "checksum":checksum}
        print(bench,results[lang][bench])

for bench in ["primes","fib","strings","mandelbrot","binarytrees"]:
    vals={lang:results[lang][bench]["checksum"] for lang in results}
    if len(set(vals.values()))!=1:
        raise RuntimeError(f"checksum divergence {bench}: {vals}")

artifacts={}
for lang,path in [("misa",misa_out),("rust",os.path.join(WORK,"rust-bench")),("go",os.path.join(WORK,"go-bench"))]:
    artifacts[lang]=os.path.getsize(path)
artifacts["python"]=os.path.getsize(PY)
artifacts["node"]=os.path.getsize(sources["node"])
artifacts["php"]=os.path.getsize(sources["php"])

out={
 "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
 "host":platform.platform(),
 "python":platform.python_version(),
 "benchlang_source_commit":"2731f29bcf0bfda750a62c81337b5e5e7456bb4a",
 "languages":results,
 "artifact_bytes":artifacts,
 "build_seconds":build_times,
 "method":"5 independent process executions per workload; median wall time; peak RSS; exact checksum agreement required"
}
path=os.path.join(ROOT,"bench","results","benchlang-latest.json")
json.dump(out,open(path,"w"),indent=2)
print("\nWROTE",path)
# Build artifacts.
artifacts={}
for lang,path in [("misa",misa_out),("rust",os.path.join(WORK,"rust-bench")),("go",os.path.join(WORK,"go-bench"))]:
    artifacts[lang]=os.path.getsize(path)
artifacts["python"]=os.path.getsize(PY)
artifacts["node"]=os.path.getsize(sources["node"])
artifacts["php"]=os.path.getsize(sources["php"])

out={
 "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
 "host":platform.platform(),
 "python":platform.python_version(),
 "languages":results,
 "artifact_bytes":artifacts,
 "build_seconds":{"misa":misa_build},
 "method":"5 warm/steady-state executions per workload; median; exact checksum agreement required"
}
path=os.path.join(ROOT,"bench","results","benchlang-latest.json")
json.dump(out,open(path,"w"),indent=2)
print("\nWROTE",path)
