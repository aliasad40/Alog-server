#!/usr/bin/env python3
"""Syslog load generator.

Produces realistic MikroTik CGNAT log lines at a controlled rate so the
documented ingestion numbers come from measurement rather than arithmetic.

    # 50k logs/sec for 60 seconds from a spoofed authorised source
    python3 scripts/loadtest.py --host 10.0.0.5 --rate 50000 --duration 60

    # As fast as the sender can go, to find the ceiling
    python3 scripts/loadtest.py --host 10.0.0.5 --rate 0 --duration 30

Run it from a *different* machine than the log server where possible: a
loopback test measures the receiver, but it also competes with it for CPU.
The source address must be authorised in the Routers tab or every packet
will be counted as unknown_router and discarded (which is itself a useful
thing to verify).
"""

from __future__ import annotations

import argparse
import os
import random
import socket
import sys
import time
from multiprocessing import Process, Queue

PROTOCOLS = ["TCP (SYN)", "TCP (ACK)", "TCP (PSH,ACK)", "TCP (FIN,ACK)",
             "UDP", "UDP", "UDP", "ICMP (type 8, code 0)"]


def build_corpus(size: int, pool_size: int, subscribers: int) -> list:
    """Pre-render lines once; the sender should measure the network path,
    not Python string formatting."""
    rnd = random.Random(1234)
    pool = [f"103.125.177.{i % 254 + 1}" for i in range(pool_size)]
    subs = [f"DT-{rnd.randint(1000, 9999)}-0{rnd.randint(3000000000, 3999999999)}"
            for _ in range(subscribers)]
    corpus = []
    for i in range(size):
        private = f"100.{rnd.randint(64, 127)}.{rnd.randint(0, 255)}.{rnd.randint(1, 254)}"
        public = pool[i % pool_size]
        dest = (f"{rnd.randint(1, 223)}.{rnd.randint(0, 255)}."
                f"{rnd.randint(0, 255)}.{rnd.randint(1, 254)}")
        sport = rnd.randint(1024, 65535)
        dport = rnd.choice([80, 443, 443, 443, 53, 22, 8080, 3478])
        proto = rnd.choice(PROTOCOLS)
        sub = rnd.choice(subs)
        line = (f"<134>Aug 30 18:33:17 BRAS-01 firewall,info forward: "
                f"in:<pppoe-{sub}> out:vlan2436, connection-state:new,snat "
                f"proto {proto}, {private}:{sport}->{dest}:{dport}, "
                f"NAT ({private}:{sport}->{public}:{sport})->{dest}:{dport}, "
                f"len {rnd.randint(40, 1500)}")
        corpus.append(line.encode())
    return corpus


def sender(host: str, port: int, rate: int, duration: float, corpus: list,
           result: Queue) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    target = (host, port)
    n = len(corpus)
    sent = 0
    errors = 0
    start = time.perf_counter()
    deadline = start + duration
    # Token-bucket pacing in 10 ms slices: tight enough to hold a rate,
    # coarse enough not to spend all the CPU on clock reads.
    slice_s = 0.01
    per_slice = int(rate * slice_s) if rate else 0
    i = 0

    while True:
        now = time.perf_counter()
        if now >= deadline:
            break
        if rate:
            slice_end = now + slice_s
            for _ in range(per_slice):
                try:
                    sock.sendto(corpus[i % n], target)
                    sent += 1
                except OSError:
                    errors += 1
                i += 1
            remaining = slice_end - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        else:
            for _ in range(2000):
                try:
                    sock.sendto(corpus[i % n], target)
                    sent += 1
                except OSError:
                    errors += 1
                i += 1

    elapsed = time.perf_counter() - start
    sock.close()
    result.put((sent, errors, elapsed))


def main() -> int:
    ap = argparse.ArgumentParser(description="MikroTik syslog load generator")
    ap.add_argument("--host", required=True, help="log server address")
    ap.add_argument("--port", type=int, default=514)
    ap.add_argument("--rate", type=int, default=10000,
                    help="logs per second across all senders; 0 = unlimited")
    ap.add_argument("--duration", type=float, default=30.0, help="seconds")
    ap.add_argument("--processes", type=int, default=0,
                    help="sender processes (default: cores/2, max 8)")
    ap.add_argument("--corpus", type=int, default=20000, help="distinct lines to cycle")
    ap.add_argument("--pool-size", type=int, default=1024, help="NAT pool addresses")
    ap.add_argument("--subscribers", type=int, default=5000)
    args = ap.parse_args()

    procs = args.processes or min(8, max(1, (os.cpu_count() or 2) // 2))
    per_proc_rate = args.rate // procs if args.rate else 0

    print(f"building corpus ({args.corpus:,} lines)...", flush=True)
    corpus = build_corpus(args.corpus, args.pool_size, args.subscribers)
    avg_len = sum(len(c) for c in corpus) / len(corpus)

    print(f"target      : {args.host}:{args.port} (UDP)")
    print(f"senders     : {procs} process(es)")
    print(f"rate        : {'unlimited' if not args.rate else f'{args.rate:,}/s'}")
    print(f"duration    : {args.duration:g}s")
    print(f"avg line    : {avg_len:.0f} bytes")
    print("sending...", flush=True)

    result: Queue = Queue()
    workers = [Process(target=sender,
                       args=(args.host, args.port, per_proc_rate, args.duration,
                             corpus, result))
               for _ in range(procs)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    total_sent = total_err = 0
    elapsed = 0.0
    while not result.empty():
        sent, errors, el = result.get()
        total_sent += sent
        total_err += errors
        elapsed = max(elapsed, el)

    mbits = total_sent * avg_len * 8 / elapsed / 1e6 if elapsed else 0
    print()
    print(f"sent        : {total_sent:,} datagrams")
    print(f"send errors : {total_err:,}")
    print(f"elapsed     : {elapsed:.2f}s")
    print(f"achieved    : {total_sent / elapsed:,.0f} logs/sec  ({mbits:,.0f} Mbit/s)")
    print()
    print("Now compare against the server's own counters:")
    print("  nls-admin status")
    print("  curl -s localhost:8088/api/health")
    print("  Settings -> System status in the web interface")
    print()
    print("UDP is lossy by design. If 'received' on the server is materially")
    print("lower than 'sent' here, the kernel dropped datagrams: check")
    print("  netstat -su | grep -i 'receive errors\\|buffer errors'")
    print("and raise net.core.rmem_max / receiver.so_rcvbuf.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
