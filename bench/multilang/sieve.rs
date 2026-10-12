use std::env;

fn main() {
    let n: usize = env::args().nth(1).unwrap_or_else(|| "500000".to_string())
        .parse().expect("n must be a non-negative integer");
    if n < 2 { println!("0 0"); return; }
    let mut prime = vec![true; n + 1];
    prime[0] = false; prime[1] = false;
    let mut p = 2usize;
    while p * p <= n {
        if prime[p] {
            let mut j = p * p;
            while j <= n { prime[j] = false; j += p; }
        }
        p += 1;
    }
    let mut count: usize = 0;
    let mut total: u64 = 0;
    for i in 2..=n {
        if prime[i] { count += 1; total += i as u64; }
    }
    println!("{} {}", count, total);
}
