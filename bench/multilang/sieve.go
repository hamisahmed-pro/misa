package main

import (
	"fmt"
	"os"
	"strconv"
)

func main() {
	n := 500000
	if len(os.Args) > 1 {
		parsed, err := strconv.Atoi(os.Args[1])
		if err != nil || parsed < 0 {
			fmt.Fprintln(os.Stderr, "n must be a non-negative integer")
			os.Exit(2)
		}
		n = parsed
	}
	if n < 2 {
		fmt.Println("0 0")
		return
	}
	prime := make([]bool, n+1)
	for i := range prime { prime[i] = true }
	prime[0], prime[1] = false, false
	for p := 2; p*p <= n; p++ {
		if prime[p] {
			for j := p*p; j <= n; j += p { prime[j] = false }
		}
	}
	count, total := 0, 0
	for i := 2; i <= n; i++ {
		if prime[i] { count++; total += i }
	}
	fmt.Printf("%d %d\n", count, total)
}
