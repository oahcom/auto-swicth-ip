import sys, re
sys.path.insert(0, '/home/administrator/auto-swicth-ip')
import daemon as d

# 回归：重启主 sing-box 只能杀主进程。WARP-WG 线各跑一份
# 'sing-box run -c .../lineN/singbox.json'，宽匹配会把 16 条一起杀掉，
# 每条线因此换会话、换出口 IP，台账绑定作废。
#
# WHY 只验纯函数：旧版把 `subprocess.run` mock 掉再跑 `restart_singbox()`，
# 只要 mock 签名一漂移就会往下走到 Popen —— 在真机上起第二份 sing-box。
# 测试不该有副作用，所以把拼参数这一步抽出来单独验。

argv = d._singbox_pkill_argv()
assert argv[:2] == ['pkill', '-f'], argv
pat = argv[2]
cfg = d.CFG['singbox']['config_file']
bin_ = d.CFG['singbox']['binary']

assert re.escape(cfg) in pat, f'pkill 模式没带配置路径: {pat!r}'
assert pat.endswith(re.escape(cfg)), pat
rx = re.compile(pat)
assert rx.match(f'{bin_} run -c {cfg}'), '模式匹配不到主进程'
assert not rx.match(f'{bin_} run -c {cfg.rsplit(chr(47), 1)[0]}/.warp/wglines/line1/singbox.json'), \
    'pkill 模式仍会误杀 WG 线'
print('T30 PASS  重启主 sing-box 不会误杀 WARP-WG 线（纯函数，无副作用）')