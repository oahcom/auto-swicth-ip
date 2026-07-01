#!/usr/bin/env python3
"""Latency benchmark using curl subprocess (real request behavior)."""

import subprocess
import statistics
import time
import sys
import os

TARGET = "https://www.gstatic.com/generate_204"
ROUNDS = 100
CONCURRENT = 10  # concurrent requests per batch

PROXIES = {
    "直接": "",
    "6696-okz": "http://127.0.0.1:6696",
    "7890-singbox": "http://127.0.0.1:7890",
}

def bench(name, proxy_url, rounds=ROUNDS):
    times = []
    errors = 0
    batch_size = CONCURRENT
    for batch_start in range(0, rounds, batch_size):
        batch_end = min(batch_start + batch_size, rounds)
        cmds = []
        for i in range(batch_start, batch_end):
            cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{time_total}", "--max-time", "10"]
            if proxy_url:
                cmd += ["-x", proxy_url]
            cmd.append(TARGET)
            cmds.append(cmd)

        # Run batch concurrently
        procs = [subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for c in cmds]
        for p in procs:
            try:
                stdout, stderr = p.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
                errors += 1
                times.append(10000)
                continue
            if p.returncode == 0 and stdout.strip():
                try:
                    times.append(float(stdout.strip()) * 1000)
                except ValueError:
                    errors += 1
                    times.append(10000)
            else:
                errors += 1
                times.append(10000)
    return times, errors

def fmt(times):
    if not times:
        return "---", "---", "---", "---", "---"
    avg = statistics.mean(times)
    med = statistics.median(times)
    p99 = sorted(times)[int(len(times)*0.99)]
    mn = min(times)
    mx = max(times)
    return f"{avg:.0f}", f"{med:.0f}", f"{mn:.0f}", f"{mx:.0f}", f"{p99:.0f}"

results = {}
for name, proxy in PROXIES.items():
    sys.stdout.write(f"Testing {name:>15}... ")
    sys.stdout.flush()
    t0 = time.time()
    times, errors = bench(name, proxy)
    elapsed = time.time() - t0
    avg, med, mn, mx, p99 = fmt(times)
    sys.stdout.write(f"avg={avg}ms median={med}ms min={mn}ms max={mx}ms p99={p99}ms err={errors}/{ROUNDS} ({elapsed:.0f}s)\n")
    sys.stdout.flush()
    results[name] = (times, errors)

print()
print(f"{'':>15} {'avg':>7} {'median':>7} {'min':>7} {'max':>7} {'p99':>7} {'err'}")
print("-" * 65)
for name in results:
    times, errors = results[name]
    avg, med, mn, mx, p99 = fmt(times)
    print(f"{name:>15} {avg:>7} {med:>7} {mn:>7} {mx:>7} {p99:>7} {errors}/{ROUNDS}")
