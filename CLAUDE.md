# auto-switch-ip 项目规范

## 大文件读取规则（防 /compact）

**daemon.py 1044行/41KB — 绝对禁止全文读取**

1. **grep/rg 定位再读**：用 `grep -n "def \|--" daemon.py | head -50` 先列函数索引
2. **只读片段**：`Read(offset=X, limit=50)` 读指定区间
3. **日志文件用 grep**：`grep "ERROR\|WARN\|rotate\|switch" daemon.log | tail -50`，绝不用 `tail -n 100` 读全量
4. **singbox-config.json 27KB**：`grep "proxy\|inbound" singbox-config.json | head -30` 定位后只读相关段
5. **proxy_429.py 19KB**：同理，grep 定位后只读片段

## 关键文件位置
- `daemon.py:1-200` — imports + config + helper
- `daemon.py:200-600` — core rotate logic
- `daemon.py:600-800` — HTTP server
- `daemon.py:800-1044` — main loop

## 验证命令
```bash
systemctl --user is-active auto-switch-ip.service
curl -m 15 -s -x http://127.0.0.1:7890 https://api.ipify.org
journalctl --user -u auto-switch-ip -n 5 --no-pager
```
