"""Single-core parser throughput benchmark. Run: python tests/parser/bench_parser.py"""
import os, sys, time, random
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
from app.parser import MikroTikParser

def make_corpus(n=200_000):
    random.seed(7)
    protos = ["TCP (SYN)", "TCP (ACK)", "UDP", "TCP (FIN,ACK)", "ICMP (type 8, code 0)"]
    out = []
    for i in range(n):
        pri = f"100.{random.randint(64,127)}.{random.randint(0,255)}.{random.randint(1,254)}"
        pub = f"103.125.177.{random.randint(1,254)}"
        dst = f"{random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
        pp, dp = random.randint(1024,65535), random.choice([80,443,53,22,8080])
        sub = f"DT-{random.randint(1000,9999)}-0{random.randint(3000000000,3999999999)}"
        p = random.choice(protos)
        out.append(
            f"<134>Aug 30 18:33:{i%60:02d} BRAS-01 firewall,info forward: in:<pppoe-{sub}> "
            f"out:vlan2436, connection-state:new,snat proto {p}, {pri}:{pp}->{dst}:{dp}, "
            f"NAT ({pri}:{pp}->{pub}:{pp})->{dst}:{dp}, len {random.randint(40,1500)}")
    return out

corpus = make_corpus()
parser = MikroTikParser()
for _ in range(20_000):           # warm up
    parser.parse(corpus[0])
t0 = time.perf_counter()
ok = 0
for line in corpus:
    log, _ = parser.parse(line)
    ok += log is not None
el = time.perf_counter() - t0
print(f"parsed   : {len(corpus):,} lines  ({ok:,} matched)")
print(f"elapsed  : {el:.3f} s")
print(f"rate     : {len(corpus)/el:,.0f} logs/sec/core")
print(f"per log  : {el/len(corpus)*1e6:.2f} us")

garbage = ["<134>Aug 30 18:33:17 host sshd: accepted publickey for root"] * 100_000
t0 = time.perf_counter()
for line in garbage:
    parser.parse(line)
el = time.perf_counter() - t0
print(f"reject   : {len(garbage)/el:,.0f} non-matching lines/sec/core")
