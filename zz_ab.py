import probe_warp as P, json, os, tempfile
from asip.proc import run as run_proc

bun = P._bun_bin()
txt = 'TCP provides reliable ordered delivery with flow control and congestion avoidance. ' * 3000
body = {'model': P.FREEMODEL, 'messages': [
    {'role': 'system', 'content': 'You are a helpful assistant.'},
    {'role': 'user', 'content': 'Reference:\n' + txt},
    {'role': 'user', 'content': 'say ok'}],
    'stream': True, 'tools': P.FINGERPRINT_TOOLS}
BODY = json.dumps(P._apply_env_marker(body))
SID = P._session_id()
print('body', len(BODY), 'sid', SID[:16], flush=True)

def send(port):
    payload = {'url': P.URL, 'method': 'POST', 'headers': P._headers(SID),
               'body': BODY, 'proxy': 'http://127.0.0.1:' + str(port),
               'timeoutMs': 20000}
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(payload, f)
    r = run_proc([bun, P.BUN_SCRIPT, path], timeout=25)
    os.remove(path)
    ls = [l for l in r.stdout.splitlines() if l.strip()]
    if not ls:
        print(port, 'NO_OUTPUT', flush=True)
        return None
    d = json.loads(ls[-1])
    st = d.get('status')
    print(port, '->', st, P.classify(st, str(d.get('head', ''))[:60]), flush=True)
    return st

ports = [7893, 7896, 7895, 7894, 7890]
res = {}
for p in ports:
    res[p] = send(p)
bad = [p for p in ports if res[p] == 429]
good = [p for p in ports if res[p] == 200]
print('BAD', bad, 'GOOD', good, flush=True)
if bad and good:
    print('REPLAY_GOOD', send(good[0]), flush=True)
    print('REPLAY_BAD', send(bad[0]), flush=True)
