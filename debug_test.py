#!/usr/bin/env python3
import logging
logging.disable(logging.CRITICAL)

import daemon
import time
import probe_warp

daemon._probe_blind_until = 0.0
daemon._WARP_PROBE_SEEN = {}
daemon._PROBE_BLOCKED_AT = {}
daemon._WARP_IP_USED = {}
daemon._last_warp_switch_ok_ts = 0.0
daemon._last_warp_reconnect_ts = 0.0
daemon._warp_cooldown_until = 0.0

daemon.CFG['warp']['refresh_model_check'] = True
daemon.CFG['warp']['probe_persistent_ttl_sec'] = 21600

def mock_probe(port, timeout):
    return ('quota', 0.1, '', None)

probe_warp.probe = mock_probe

member = {'tag': 'W1', 'port': 1080}
ip = 'ip-q'

result = daemon._warp_model_usable(member, ip)

print(f'Result: {result}')
print(f'_WARP_PROBE_SEEN: {daemon._WARP_PROBE_SEEN}')
print(f'_WARP_IP_USED: {daemon._WARP_IP_USED}')
print(f'_probe_cache_get: {daemon._probe_cache_get("W1", "ip-q")}')
print(f'_PROBE_BLOCKED_AT: {daemon._PROBE_BLOCKED_AT}')