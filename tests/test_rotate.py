import sys, time
sys.path.insert(0, '/home/administrator/auto-swicth-ip')
import daemon as d

# 只覆盖 _rotate_once 自己的语义：合并 / 限速 / 无备线 / 成功后拉黑旧出口。
# 换线本体（_reconnect_warp_by_table）另有测试，这里一律 monkeypatch 掉。

# ---- T4: reason 白名单 ----
cases = [('429 quota', 'rate429'), ('rate429', 'rate429'), ('abuse403', 'abuse403'),
         ('403 forbidden', 'abuse403'), ('proactive', 'proactive'),
         ('whatever', 'rate429'), ('', 'rate429'), (None, 'rate429')]
for raw, want in cases:
    got = d._normalize_rotate_reason(raw)
    assert got == want, f'reason {raw!r}: {got} != {want}'
print('T4 PASS  reason 白名单')

# ---- T6: 合并窗口 ----
d._last_rotate_ts = time.time()
r = d._rotate_once('rate429')
assert r['reason'] == 'coalesced' and r['ok'] is True, r
assert r['switched'] is False and r['switched_at'] > 0, r
print('T6 PASS  800ms 内合并')

# ---- T7: 限速 ----
d._last_rotate_ts = time.time() - 2.0
r = d._rotate_once('rate429')
assert r['reason'] == 'too_frequent' and r['ok'] is False, r
print('T7 PASS  8s 内拒绝')

# ---- T7b: 无备线 ----
d._last_rotate_ts = 0
d._reconnect_warp_by_table = lambda reason, interrupt=True, out=None: False
r = d._rotate_once('rate429')
assert r['reason'] == 'no_warm_standby', r
print('T7b PASS  无备线')

# ---- T19/T19b/T21b: 换下来的出口要拉黑，主动轮换不拉黑 ----
marked = []
d._mark_warp_ip_used = lambda ip, now, blocked=False, kind=None, forget_probe=True: marked.append((ip, blocked, kind))
d._warp_line_ip = lambda tag: '2a09::old'
d.active_warp_tag = lambda: 'WARP-CF-1'
def fake_reconnect(reason, interrupt=True, out=None):
    out['tag'] = 'WARP-CF-9'
    out['ip'] = '2a09::new'
    out['switched_at'] = 12345.0
    return True
d._reconnect_warp_by_table = fake_reconnect

for reason, want_kind in (('rate429', 'rate429'), ('abuse403', 'abuse403')):
    marked.clear()
    d._last_rotate_ts = 0
    r = d._rotate_once(reason)
    assert r['ok'] and r['switched'], r
    assert r['tag'] == 'WARP-CF-9' and r['egress'] == '2a09::new', r
    assert r['switched_at'] == 12345.0 and d._last_rotate_ts == 12345.0, r
    assert marked == [('2a09::old', True, want_kind)], (reason, marked)
print('T19 PASS  429/403 换线拉黑旧出口')

marked.clear()
d._last_rotate_ts = 0
r = d._rotate_once('proactive')
assert r['ok'] and marked == [], marked
print('T21b PASS  主动轮换不拉黑')
