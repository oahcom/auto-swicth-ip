import sys, time
sys.path.insert(0, '/home/administrator/auto-swicth-ip')
import daemon as d

# 覆盖探测台账的两层去重：同线同址命中、跨线同址复用（结论是地址的属性）。
# WHY 这条要单独测：v4 池按 colo 共享，两条线完全可能出口同一个地址
# （实测 104.28.222.43 同时挂在 WG-1 与 WG-9），而探测是烧配额的。

ip = '104.28.222.43'
other = '2a09:bac5:1efd:2da5::48c:5e'

def reset():
    d._WARP_PROBE_SEEN.clear()
    d._PROBE_STATS.update(hit=0, miss=0, cross_tag_reuse=0, expired=0)

# ---- P1 同一条线 + 同一个地址：TTL 内命中，不重探 ----
reset()
now = time.time()
d._probe_cache_put('WARP-WG-1', ip, 'OK', now=now)
assert d._probe_cache_get('WARP-WG-1', ip, now + 5) == 'OK'
assert d._PROBE_STATS['hit'] == 1, d._PROBE_STATS
print('P1 PASS  同线同址 TTL 内命中')

# ---- P2 换一条线、同一个地址：复用结论，不再探一次 ----
assert d._probe_cache_get('WARP-WG-9', ip, now + 6) == 'OK'
assert d._PROBE_STATS['cross_tag_reuse'] == 1, d._PROBE_STATS
assert d._PROBE_STATS['miss'] == 0, d._PROBE_STATS
print('P2 PASS  跨线同址复用（不重复烧配额）')

# ---- P3 TTL 过期后必须重探 ----
t = d._probe_ttl('OK')
assert d._probe_cache_get('WARP-WG-9', ip, now + t + 1) is None
assert d._PROBE_STATS['cross_tag_reuse'] == 1, d._PROBE_STATS
print('P3 PASS  过期后重探，不再复用')

# ---- P4 别的地址不受影响 ----
before = d._PROBE_STATS['miss']
assert d._probe_cache_get('WARP-WG-9', other, now + 6) is None
assert d._PROBE_STATS['miss'] == before + 1, d._PROBE_STATS
print('P4 PASS  其它地址照常重探')

# ---- P5 两条线结论不同：取最近的那条证据 ----
reset()
d._probe_cache_put('WARP-WG-1', ip, 'OK', now=now)
d._probe_cache_put('WARP-WG-9', ip, 'quota', now=now + 10)
got = d._probe_cache_get('WARP-CF-B', ip, now + 12)
assert got == 'quota', got
print('P5 PASS  冲突结论取最近')

# ---- P6 该地址被 403/429 标脏后，所有线的结论一起作废 ----
d._probe_cache_forget(ip)
assert d._probe_cache_get('WARP-WG-1', ip, now + 12) is None
print('P6 PASS  标脏后跨线全清')
# ---- P7 隧道故障类结论（unreachable）不跨线复用 ----
reset()
d._probe_cache_put('WARP-WG-1', ip, 'unreachable', now=now)
assert d._probe_cache_get('WARP-WG-9', ip, now + 3) is None
assert d._PROBE_STATS['cross_tag_reuse'] == 0, d._PROBE_STATS
print('P7 PASS  unreachable 不跨线复用')

# ---- P8 TTL 内的 OK 直接复用（主人 2026-10-28：旧 OK 被切上去可接受） ----
reset()
d._probe_cache_put('WARP-WG-1', ip, 'OK', now=now)
assert d._probe_cache_lookup('WARP-WG-1', ip, now + 500)[0] == 'OK'
assert d._probe_cache_lookup('WARP-WG-1', ip, now + 700) is None
print('P8 PASS  TTL 内 OK 复用，过期即重探')
