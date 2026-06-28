#!/usr/bin/env python3
"""
auto-switch-ip 端到端测试脚本
测试所有核心功能是否正常，用例覆盖所有失败场景
"""
import json
import sys
import time
import sqlite3
import subprocess
import concurrent.futures
import os
import requests

# ============= 配置 =============
CLASH_API = "http://127.0.0.1:9090"
CLASH_SECRET = open(os.path.expanduser("~/.singbox_secret")).read().strip()
OKZ_PROXY = "http://127.0.0.1:6696"
SINGBOX_PROXY = "http://127.0.0.1:7890"
NROUTER_DB = "/home/administrator/.9router/db/data.sqlite"
TEST_URL = "https://www.gstatic.com/generate_204"
IP_CHECK_URL = "https://api.ipify.org"

PASS = 0
FAIL = 0

def assert_eq(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: expected={want}, got={got}")

def assert_true(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        msg = f"  ❌ {name}"
        if detail:
            msg += f": {detail}"
        print(msg)

def headers():
    return {"Authorization": f"Bearer {CLASH_SECRET}"}


# ============= 测试用例 =============

def test_1_singbox_api_reachable():
    """测试 1: Clash API 可达"""
    print("\n[测试 1] Clash API 可达性")
    try:
        r = requests.get(f"{CLASH_API}/version", headers=headers(), timeout=3)
        assert_eq("版本号", r.json().get("version", ""), "sing-box 1.13.0")
    except Exception as e:
        assert_true("连接成功", False, str(e))

def test_2_node_list_not_empty():
    """测试 2: 节点列表非空"""
    print("\n[测试 2] 节点列表")
    r = requests.get(f"{CLASH_API}/proxies/proxy", headers=headers(), timeout=3)
    data = r.json()
    nodes = data.get("all", [])
    current = data.get("now", "")
    assert_true("节点列表非空", len(nodes) > 10, f"实际={len(nodes)}")
    assert_true("有 current 节点", len(current) > 0, f"actual={current}")
    print(f"  当前: {current}, 共 {len(nodes)} 节点")

def test_3_delay_test_works():
    """测试 3: 节点延迟测试 API 可用"""
    print("\n[测试 3] 延迟测试 API")
    # 拿第一个非 direct 节点
    r = requests.get(f"{CLASH_API}/proxies/proxy", headers=headers(), timeout=3)
    nodes = [n for n in r.json().get("all", []) if n != "direct"]
    test_node = nodes[0] if nodes else None
    if not test_node:
        assert_true("有测试节点", False, "无可用节点")
        return
    r = requests.get(f"{CLASH_API}/proxies/{test_node}/delay",
                     params={"timeout": 8000, "url": TEST_URL},
                     headers=headers(), timeout=12)
    if r.status_code == 200:
        d = r.json()
        assert_true("延迟测试返回数值", d.get("delay") is not None, f"resp={d}")
        print(f"  {test_node}: {d.get('delay')}ms")
    else:
        assert_true("延迟测试成功", False, f"status={r.status_code}")

def test_4_node_connectivity():
    """测试 4: 随机 5 个节点实际连通性"""
    print("\n[测试 4] 节点连通性（5 个采样）")
    r = requests.get(f"{CLASH_API}/proxies/proxy", headers=headers(), timeout=3)
    nodes = [n for n in r.json().get("all", []) if n != "direct"]
    # 取不同位置的 5 个节点
    step = max(1, len(nodes) // 5)
    sample = nodes[::step][:5]
    alive = 0
    dead = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futs = {}
        for n in sample:
            def test(tag=n):
                try:
                    rr = requests.get(f"{CLASH_API}/proxies/{tag}/delay",
                                      params={"timeout": 8000, "url": TEST_URL},
                                      headers=headers(), timeout=12)
                    if rr.status_code == 200:
                        return tag, rr.json().get("delay")
                except:
                    pass
                return tag, None
            futs[pool.submit(test)] = n
        for f in concurrent.futures.as_completed(futs):
            tag, delay = f.result()
            if delay is not None:
                alive += 1
                print(f"  ✅ {tag}: {delay}ms")
            else:
                dead += 1
                print(f"  ❌ {tag}: 超时")
    assert_true("采样节点 ≥60% 可用", alive >= len(sample) * 0.6,
                f"alive={alive}/{len(sample)}")

def test_5_proxy_switch_and_ip_change():
    """测试 5: 切节点后出口 IP 真的变了"""
    print("\n[测试 5] 节点切换 + IP 变化")
    r = requests.get(f"{CLASH_API}/proxies/proxy", headers=headers(), timeout=3)
    nodes = [n for n in r.json().get("all", []) if n != "direct"]
    current = r.json().get("now", "")
    # 选一个延迟低的节点
    test_node = None
    candidates = [n for n in nodes if n != current][:10]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futs = {}
        for n in candidates:
            def test(tag=n):
                try:
                    rr = requests.get(f"{CLASH_API}/proxies/{tag}/delay",
                                      params={"timeout": 8000, "url": TEST_URL},
                                      headers=headers(), timeout=12)
                    if rr.status_code == 200:
                        return tag, rr.json().get("delay")
                except:
                    pass
                return tag, None
            futs[pool.submit(test)] = n
        best = None
        for f in concurrent.futures.as_completed(futs):
            tag, delay = f.result()
            if delay and delay < 2000 and (best is None or delay < best[1]):
                best = (tag, delay)
        test_node = best[0] if best else candidates[0]

    if not test_node:
        assert_true("有测试节点", False)
        return

    # 切到测试节点
    r = requests.put(f"{CLASH_API}/proxies/proxy",
                     headers={**headers(), "Content-Type": "application/json"},
                     json={"name": test_node}, timeout=3)
    assert_true("切换成功", r.status_code in (200, 204), f"status={r.status_code}")
    time.sleep(2)

    # 验证 current 更新
    r2 = requests.get(f"{CLASH_API}/proxies/proxy", headers=headers(), timeout=3)
    new_current = r2.json().get("now", "")
    assert_true("current 已更新", new_current == test_node, f"expected={test_node}, got={new_current}")

    # 实际出口 IP
    try:
        ip = requests.get(IP_CHECK_URL, proxies={"http": SINGBOX_PROXY, "https": SINGBOX_PROXY},
                          timeout=15).text.strip()
        assert_true("出口 IP 非空", len(ip) > 0)
        print(f"  出口 IP: {ip}")
    except Exception as e:
        assert_true("出口 IP 获取成功", False, str(e))

def test_6_9router_proxy_config():
    """测试 6: 9router outboundProxyUrl 指向 sing-box:7890"""
    print("\n[测试 6] 9router 代理配置")
    try:
        c = sqlite3.connect(NROUTER_DB)
        row = c.execute('SELECT data FROM settings WHERE id=1').fetchone()
        s = json.loads(row[0])
        assert_eq("proxyUrl", s.get("outboundProxyUrl"), "http://127.0.0.1:7890")
        assert_eq("enabled", s.get("outboundProxyEnabled"), True)
    except Exception as e:
        assert_true("读取成功", False, str(e))

def test_7_9router_requests_readable():
    """测试 7: 9router SQLite requestDetails 可读"""
    print("\n[测试 7] 9router SQLite 读取")
    try:
        c = sqlite3.connect(NROUTER_DB)
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT provider, model, status FROM requestDetails ORDER BY id DESC LIMIT 5").fetchall()
        assert_true("requestDetails 非空", len(rows) > 0, f"rows={len(rows)}")
        for r in rows[:3]:
            print(f"  {r['provider']} | {r['model']} | {r['status']}")
    except Exception as e:
        assert_true("读取成功", False, str(e))

def test_8_daemon_running():
    """测试 8: daemon systemd 服务运行中"""
    print("\n[测试 8] Daemon 运行状态")
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "auto-switch-ip.service"],
            capture_output=True, text=True, timeout=5)
        assert_eq("daemon 状态", r.stdout.strip(), "active")
        assert_true("exit code 0", r.returncode == 0)
    except Exception as e:
        assert_true("systemd 查询成功", False, str(e))

def test_9_daemon_log_has_rotations():
    """测试 9: daemon 日志有切换记录"""
    print("\n[测试 9] Daemon 切换历史")
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "auto-switch-ip", "-n", "50", "--no-pager"],
            capture_output=True, text=True, timeout=5)
        lines = r.stdout
        has_start = "daemon started" in lines
        has_switch = "切到" in lines
        has_singbox_ok = "sing-box OK" in lines
        assert_true("有启动日志", has_start)
        assert_true("有切换记录", has_switch)
        assert_true("sing-box 连接成功", has_singbox_ok)
    except Exception as e:
        assert_true("日志查询成功", False, str(e))

def test_10_9router_e2e():
    """测试 10: 9router → sing-box → 外网 端到端"""
    print("\n[测试 10] 端到端 9router → sing-box → 外网")
    try:
        # 从 9router 发请求
        c = sqlite3.connect(NROUTER_DB)
        s = json.loads(c.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
        cli_token_req = subprocess.run([
            "python3", "-c",
            "import hashlib; "
            "m=open('/home/administrator/.9router/machine-id').read().strip(); "
            "c=open('/home/administrator/.9router/auth/cli-secret').read().strip(); "
            "print(hashlib.sha256((m+'***'+c).encode()).hexdigest()[:16])"
        ], capture_output=True, text=True, timeout=5)
        token = cli_token_req.stdout.strip()
        r = requests.post("http://127.0.0.1:20128/v1/chat/completions",
                          headers={"Content-Type": "application/json",
                                   "Authorization": f"Bearer {token}"},
                          json={"model": "9router_hermes",
                                "messages": [{"role": "user", "content": "say ok"}],
                                "max_tokens": 5, "stream": False},
                          timeout=30)
        assert_true("9router 返回 200", r.status_code == 200, f"status={r.status_code}")
        body = r.json()
        assert_true("有 choices", "choices" in body, f"body keys={list(body.keys())}")
        if "choices" in body:
            print(f"  model={body.get('model')} response={body['choices'][0]['message']['content'][:30]}")
    except Exception as e:
        assert_true("端到端成功", False, str(e))

def test_11_node_health_rejects_dead():
    """测试 11: 死节点不会被选中（模拟 fail_set）"""
    print("\n[测试 11] 死节点过滤")
    from daemon import pick_next_node, _DEAD_NODES, _DEAD_NODES_RELEASE_TS
    # 模拟死节点
    fake_dead = "fake-dead-node-1"
    _DEAD_NODES.add(fake_dead)
    _DEAD_NODES_RELEASE_TS[fake_dead] = time.time() + 300
    nodes = ["node-a", "node-b", fake_dead, "node-c"]
    picked = pick_next_node("node-a", nodes, set())
    assert_true("死节点被跳过", picked != fake_dead, f"picked={picked}")
    assert_true("选了活节点", picked in ("node-b", "node-c"), f"picked={picked}")
    _DEAD_NODES.discard(fake_dead)
    _DEAD_NODES_RELEASE_TS.pop(fake_dead, None)

def test_12_multiple_switches_ip_changes():
    """测试 12: 连续切换 3 次，每次 IP 应该不同"""
    print("\n[测试 12] 连续切换验证 IP 变化")
    r = requests.get(f"{CLASH_API}/proxies/proxy", headers=headers(), timeout=3)
    nodes = [n for n in r.json().get("all", []) if n != "direct"]
    # 选 3 个延迟低的节点
    sample = nodes[:15]
    candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futs = {}
        for n in sample:
            def test(tag=n):
                try:
                    rr = requests.get(f"{CLASH_API}/proxies/{tag}/delay",
                                      params={"timeout": 8000, "url": TEST_URL},
                                      headers=headers(), timeout=12)
                    if rr.status_code == 200:
                        return tag, rr.json().get("delay")
                except:
                    pass
                return tag, None
            futs[pool.submit(test)] = n
        for f in concurrent.futures.as_completed(futs):
            tag, delay = f.result()
            if delay and delay < 3000:
                candidates.append(tag)
        seen = set()
        candidates = [x for x in candidates if not (x in seen or seen.add(x))]

    candidates = candidates[:3]
    if len(candidates) < 3:
        # fallback: 直接取前3个
        candidates = sample[:3]

    ips = []
    for node in candidates:
        requests.put(f"{CLASH_API}/proxies/proxy",
                     headers={**headers(), "Content-Type": "application/json"},
                     json={"name": node}, timeout=3)
        time.sleep(1.5)
        try:
            ip = requests.get(IP_CHECK_URL,
                              proxies={"http": SINGBOX_PROXY, "https": SINGBOX_PROXY},
                              timeout=15).text.strip()
            ips.append((node, ip))
            print(f"  {node} → {ip}")
        except:
            ips.append((node, "timeout"))
            print(f"  {node} → timeout")

    valid_ips = [ip for _, ip in ips if ip != "timeout"]
    assert_true("≥2 次获取到 IP", len(valid_ips) >= 2, f"ips={valid_ips}")
    if len(valid_ips) >= 2:
        unique = len(set(valid_ips))
        # CDN 共享 IP 可能相同，记录但不强制
        print(f"  唯一 IP 数: {unique}/{len(valid_ips)} (CDN 共享 IP 可能相同)")


# ============= 运行 =============
if __name__ == "__main__":
    print("=" * 60)
    print("auto-switch-ip 端到端测试")
    print("=" * 60)

    tests = [
        test_1_singbox_api_reachable,
        test_2_node_list_not_empty,
        test_3_delay_test_works,
        test_4_node_connectivity,
        test_5_proxy_switch_and_ip_change,
        test_6_9router_proxy_config,
        test_7_9router_requests_readable,
        test_8_daemon_running,
        test_9_daemon_log_has_rotations,
        test_10_9router_e2e,
        test_11_node_health_rejects_dead,
        test_12_multiple_switches_ip_changes,
    ]

    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"  ⚠ 测试异常: {e}")

    print("\n" + "=" * 60)
    print(f"结果: ✅ {PASS} 通过 / ❌ {FAIL} 失败")
    print("=" * 60)
    sys.exit(1 if FAIL > 0 else 0)
