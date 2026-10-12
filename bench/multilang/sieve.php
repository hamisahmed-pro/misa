<?php
declare(strict_types=1);
$n = isset($argv[1]) ? (int)$argv[1] : 500000;
if ($n < 2) { echo "0 0\n"; exit(0); }
$prime = array_fill(0, $n + 1, true);
$prime[0] = false; $prime[1] = false;
for ($p = 2; $p * $p <= $n; $p++) {
    if ($prime[$p]) {
        for ($j = $p * $p; $j <= $n; $j += $p) $prime[$j] = false;
    }
}
$count = 0; $total = 0;
for ($i = 2; $i <= $n; $i++) {
    if ($prime[$i]) { $count++; $total += $i; }
}
echo $count . " " . $total . "\n";
