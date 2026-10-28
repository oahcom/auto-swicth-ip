import sys, re
sys.path.insert(0, '/home/administrator/auto-swicth-ip')
import daemon as d

# 回归：重启主 sing-box 只能杀主进程。WARP-WG 线各跑一份
# 'sing-box run -c .../lineN/singbox.json'，宽匹配会把 16 条一起杀掉，
# 每条线因此换会话、换出口 IP，台账绑定作废。
calls = []
class FakeRun:
    def __init__(self, *a, **k): pass
    def __call__(self, argv, **k):
        calls.append(list(argv))
        class R: returncode = 0
        return R()
d.subprocess.run = FakeRun()
d._set_outbound_proxy = lambda enabled: None
d._current_mode = lambda: (None, None)
d.os.path.getsize = lambda p: 0

try:
    d.restart_singbox()
except Exception:
    pass  # 后续 Popen 起不来无妨，只验 pkill 那一步

pk = [c for c in calls if c and c[0] == 'pkill']
assert pk, '没有调用 pkill'
pat = pk[0][2]
cfg = d.CFG['singbox']['config_file']
bin_ = d.CFG['singbox']['binary']
assert re.escape(cfg) in pat, f'pkill 模式没带配置路径: {pat!r}'
assert pat.endswith(re.escape(cfg)), pat
rx = re.compile(pat)
assert rx.match(f'{bin_} run -c {cfg}')
assert not rx.match(f'{bin_} run -c /home/administrator/auto-swicth-ip/.warp/wglines/line1/singbox.json'), \
    'pkill 模式仍会误杀 WG 线'
print('T30 PASS  重启主 sing-box 不会误杀 WARP-WG 线')