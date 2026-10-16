"""
End to end smoke check. No chain, no real oracle, no network beyond localhost.

    python tests/smoke.py

Runs a mock oracle, real MinerServers and the real validator cycle over HTTP, plus
config, windows, protocol, scoring and weight-setting checks. Throwaway wallets and
the SQLite db live in a temp dir that is deleted on exit. Prints ALL SMOKE CHECKS PASSED.
"""

import atexit, json, os, random, shutil, sys, tempfile, threading, types
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bittensor as bt
import requests

TMP = tempfile.mkdtemp(prefix="watch_oracle_smoke_")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
KEY = "test-key-should-never-print"  # fake, only valid against the mock oracle below
calls = {"floor": 0, "flaky": 0}

class OracleMock(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("content-type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(body).encode())
    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if self.headers.get("x-oracle-key") != KEY: return self._send(401, {"error": "unauthorized"})
        if u.path == "/api/oracle/universe":
            return self._send(200, {"refs": [{"ref": r, "unique_listings": 10} for r in ["A", "B", "C"]]})
        if u.path == "/api/oracle/floor":
            calls["floor"] += 1
            ref = q["ref"]; assert set(q) == {"ref", "start", "days"}, q
            if ref == "C": return self._send(404, {"error": "insufficient_data"})
            if ref == "RL": return self._send(429, {}, {"Retry-After": "0"})
            if ref == "FLAKY":
                calls["flaky"] += 1
                if calls["flaky"] < 3: return self._send(503, {})
            days = int(q["days"]) + (1 if ref == "MISMATCH" else 0)
            return self._send(200, {"ref": ref, "window_start": q["start"], "window_days": days,
                                    "floor_usd": 10000.0 if int(q["days"]) == 1 else 20000.0, "sample_count": 5})
        self._send(404, {"error": "not_found"})

srv = HTTPServer(("127.0.0.1", 0), OracleMock); threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["ORACLE_API_KEY"] = KEY
os.environ["ORACLE_BASE_URL"] = f"http://127.0.0.1:{srv.server_port}"

import watch_oracle.oracle_client as oc
oc.BACKOFF_BASE_SECONDS = 0.01
from watch_oracle.oracle_client import OracleAuthError, OracleClient, OracleResponseError, OracleUnavailableError
from watch_oracle.protocol import ENDPOINT, FloorPrediction, ProtocolError
from watch_oracle.transport import MinerServer, MinerTarget, query_miners
from watch_oracle.utils import windows as w
from watch_oracle.utils.uids import get_miner_targets
from watch_oracle.validator import forward as fw
from watch_oracle.validator.reward import score_prediction, scores_vector
from watch_oracle.validator.storage import ValidatorStore

UTC = timezone.utc
# --- windows
assert w.ANCHOR.weekday() == 0
for n in range(1, 8):
    for offset in range(-20, 400):
        d = date(2026, 1, 1) + timedelta(days=offset)
        s = w.window_start(d, n)
        assert s <= d < s + timedelta(days=n) and (s - w.ANCHOR).days % n == 0
    assert w.window_start(date(1960, 3, 3), n) <= date(1960, 3, 3)          # pre-anchor floors correctly
for offset in range(60):
    assert w.window_start(date(2026, 9, 1) + timedelta(days=offset), 7).weekday() == 0   # 7 -> Mondays
assert w.window_start(date(2026, 10, 16), 1) == date(2026, 10, 16)                         # 1 -> every day
thu = datetime(2026, 10, 15, 12, tzinfo=UTC)
assert w.target_window_start(thu, 7, 7) == date(2026, 10, 19)
assert w.target_window_start(thu, 1, 7) == date(2026, 10, 12)
assert w.target_window_start(thu, 1, 1) == date(2026, 10, 16)
assert w.latest_closed_window_start(thu, 7) == date(2026, 10, 5)
assert w.latest_closed_window_start(thu, 1) == date(2026, 10, 14)
midnight = datetime(2026, 10, 19, 0, tzinfo=UTC)
assert w.latest_closed_window_start(midnight, 7) == date(2026, 10, 12)                     # closes exactly at start + n
assert w.latest_closed_window_start(midnight - timedelta(microseconds=1), 7) == date(2026, 10, 5)
assert w.rolling_window_start(thu, 7, 28) == date(2026, 9, 14)
assert w.rolling_window_start(thu, 1, 28) == date(2026, 9, 17)
s3 = w.window_start(date(2026, 10, 15), 3); assert (s3 - w.ANCHOR).days % 3 == 0

# --- oracle client
c = OracleClient()
assert KEY not in repr(c)
assert [u.ref for u in c.get_universe()] == ["A", "B", "C"]
f = c.get_floor("A", date(2026, 10, 14), 1)
assert (f.floor_usd, f.window_start, f.window_days) == (10000.0, date(2026, 10, 14), 1)
assert c.get_floor("C", date(2026, 10, 14), 1) is None
assert c.get_floor("FLAKY", date(2026, 10, 14), 1).floor_usd == 10000.0 and calls["flaky"] == 3
for bad, exc in [("RL", OracleUnavailableError), ("MISMATCH", OracleResponseError)]:
    try: c.get_floor(bad, date(2026, 10, 14), 1); raise SystemExit(f"expected {exc}")
    except exc as e: assert KEY not in str(e)
try: OracleClient(api_key="wrong").get_universe(); raise SystemExit("expected auth")
except OracleAuthError as e: assert "wrong" not in str(e)
try: OracleClient(api_key=""); raise SystemExit("expected missing key")
except OracleAuthError: pass

# --- reward
assert score_prediction(10000, 10000, 0.15) == (0.0, 1.0)
e, s = score_prediction(10750, 10000, 0.15); assert abs(e - 0.075) < 1e-9 and abs(s - 0.5) < 1e-9
assert score_prediction(12000, 10000, 0.15)[1] == 0.0
assert score_prediction(None, 10000, 0.15) == (None, 0.0)

# --- protocol
rt = FloorPrediction.from_bytes(FloorPrediction(refs=["A"], horizon_days=1, window_days=3, predictions={"A": 1.5}).to_bytes())
assert (rt.refs, rt.horizon_days, rt.window_days, rt.predictions) == (["A"], 1, 3, {"A": 1.5})
assert FloorPrediction.from_bytes(b"{}").window_days == 7
for raw in [b"nope", b"[]", b'{"refs": "A"}', b'{"horizon_days": true}', b'{"predictions": [1]}', b'{"window_days": 0}', b'{"window_days": 8}', b'{"window_days": true}', b'{"window_days": "1"}', b'{"window_days": 1.0}']:
    try: FloorPrediction.from_bytes(raw); raise SystemExit(f"expected ProtocolError for {raw}")
    except ProtocolError: pass

# --- extract_predictions
P = lambda preds: FloorPrediction(refs=["A", "B"], predictions=preds)
assert fw.extract_predictions(P({"A": 1.0, "Z": 2.0}), ["A", "B"]) == {"A": 1.0}
assert fw.extract_predictions(None, ["A", "B"]) is None
assert fw.extract_predictions(P(None), ["A", "B"]) is None
for bad in [float("nan"), float("inf"), -5.0, 0, "1", True]:
    assert fw.extract_predictions(P({"A": bad}), ["A", "B"]) is None, bad

# --- wallets (throwaway, in TMP)
wpath = os.path.join(TMP, "wallets")
def wallet(name):
    wl = bt.Wallet(name="smoke", hotkey=name, path=wpath)
    try: wl.hotkey
    except Exception: wl.create_new_hotkey(use_password=False, overwrite=True, suppress=True)
    return wl
vw, mw, mw2, mw3, ow = (wallet(n) for n in ["validator", "miner", "miner2", "miner3", "outsider"])
VHK = vw.hotkey.ss58_address

# --- miners: real Miner.forward + real base blacklist, served by real MinerServer
import neurons.miner as nm
from watch_oracle.base.miner import BaseMinerNeuron

def make_miner(wl, noise):
    m = object.__new__(nm.Miner)
    m.oracle = c; m.noise = noise; m._rng = random.Random(0)
    m._pool = ThreadPoolExecutor(4); m._cache_lock = threading.Lock(); m._floor_cache = {}
    m.config = types.SimpleNamespace(blacklist=types.SimpleNamespace(allow_non_registered=False, force_validator_permit=True))
    return m

registered = {VHK: types.SimpleNamespace(validator_permit=True), ow.hotkey.ss58_address: types.SimpleNamespace(validator_permit=False)}
mg_stub = types.SimpleNamespace(by_hotkey=lambda hk: registered.get(hk))
m_perfect = make_miner(mw, 0.0); m_perfect.metagraph = mg_stub
s_perfect = MinerServer(mw, "127.0.0.1", 0, m_perfect.forward, m_perfect.blacklist); s_perfect.start()

def biased(req):  # every requested ref predicted 7.5% high, so score 0.5 where truth exists
    req.predictions = {r: 10750.0 for r in req.refs}; return req
s_biased = MinerServer(mw2, "127.0.0.1", 0, biased, lambda hk: None); s_biased.start()
def malformed(req):
    req.predictions = {"A": float("nan")}; return req
s_bad = MinerServer(mw3, "127.0.0.1", 0, malformed, lambda hk: None); s_bad.start()
ep = lambda s: f"127.0.0.1:{s._httpd.server_address[1]}"

# blacklist logic
assert m_perfect.blacklist(VHK) is None
assert m_perfect.blacklist(ow.hotkey.ss58_address) == "no_validator_permit"
assert m_perfect.blacklist("5Gunknown") == "unrecognized_hotkey"

# transport: success, gate rejection, wrong receiver, unreachable
req = FloorPrediction(refs=["A", "B", "C", "A"], horizon_days=1, window_days=1)
out = query_miners(vw, [MinerTarget(0, mw.hotkey.ss58_address, ep(s_perfect))], req, timeout=5)
assert out[0].predictions == {"A": 10000.0, "B": 10000.0}, out
assert query_miners(ow, [MinerTarget(0, mw.hotkey.ss58_address, ep(s_perfect))], req, timeout=5) == {0: None}
assert query_miners(vw, [MinerTarget(0, mw2.hotkey.ss58_address, ep(s_perfect))], req, timeout=5) == {0: None}
assert query_miners(vw, [MinerTarget(0, mw.hotkey.ss58_address, "127.0.0.1:1")], req, timeout=2) == {0: None}
assert query_miners(vw, [], req, timeout=1) == {}

# direct HTTP: tampered body, replay, wrong path, unsigned
body = req.to_bytes(); url = f"http://{ep(s_perfect)}{ENDPOINT}"
h = bt.http_auth.sign(vw, method="POST", path=ENDPOINT, body=body, receiver_ss58=mw.hotkey.ss58_address)
assert requests.post(url, data=body + b" ", headers=h).status_code == 401
h = bt.http_auth.sign(vw, method="POST", path=ENDPOINT, body=body, receiver_ss58=mw.hotkey.ss58_address)
assert requests.post(url, data=body, headers=h).status_code == 200
assert requests.post(url, data=body, headers=h).status_code == 401                         # replay
assert requests.post(url, data=body).status_code == 401                                    # unsigned
assert requests.post(f"http://{ep(s_perfect)}/other", data=body).status_code == 404
before = calls["floor"]; query_miners(vw, [MinerTarget(0, mw.hotkey.ss58_address, ep(s_perfect))], req, timeout=5)
assert calls["floor"] == before                                                            # miner cache incl 404
# per-(window_start, window_days) cache: different validator settings never collide
def ask(days, refs=("A", "B")):
    r = query_miners(vw, [MinerTarget(0, mw.hotkey.ss58_address, ep(s_perfect))], FloorPrediction(refs=list(refs), horizon_days=1, window_days=days), timeout=5)
    return r[0].predictions
assert ask(7) == {"A": 20000.0, "B": 20000.0}                  # 7-day bucket, fetched with days=7
assert ask(1) == {"A": 10000.0, "B": 10000.0}                  # 1-day bucket untouched by the 7-day one
before = calls["floor"]; assert ask(7) == {"A": 20000.0, "B": 20000.0} and ask(1) == {"A": 10000.0, "B": 10000.0}
assert calls["floor"] == before                                # both buckets served from cache
assert set(k[1] for k in m_perfect._floor_cache) == {1, 7}
stale = (date(2000, 1, 3), 7); m_perfect._floor_cache[stale] = {"A": 1.0}
ask(7); assert stale not in m_perfect._floor_cache              # older window of same length evicted
assert any(k[1] == 1 for k in m_perfect._floor_cache)          # other lengths kept
m_perfect.noise = 0.1; vals = [m_perfect._apply_noise(100.0) for _ in range(1000)]
assert 90 <= min(vals) and max(vals) <= 110 and len(set(vals)) > 1; m_perfect.noise = 0.0

# --- uids / targets
Bal = lambda a: types.SimpleNamespace(amount=a)
N = lambda uid, hk, axon, permit=False, stake=0.0: types.SimpleNamespace(uid=uid, hotkey=hk, axon=axon, validator_permit=permit, total_stake=Bal(stake))
neurons = [N(0, mw.hotkey.ss58_address, ep(s_perfect)), N(1, mw2.hotkey.ss58_address, ep(s_biased)),
           N(2, "5Gdead", "127.0.0.1:1"), N(3, mw3.hotkey.ss58_address, ep(s_bad)),
           N(4, VHK, "127.0.0.1:9", True, 10_000.0), N(5, "5Gnoaxon", None), N(6, "5Gbigval", "1.2.3.4:5", True, 99_999.0)]
meta = types.SimpleNamespace(neurons=neurons, hotkeys=[n.hotkey for n in neurons])
assert [t.uid for t in get_miner_targets(meta, 4096, exclude=[4])] == [0, 1, 2, 3]

# --- full validator cycle over real HTTP, 1-day windows
tmp = TMP
cfg = types.SimpleNamespace(neuron=types.SimpleNamespace(batch_size=50, horizon_days=1, window_days=1, timeout=3,
      vpermit_tao_limit=4096, round_interval=3600, error_band=0.15, rolling_window_days=28))
v = types.SimpleNamespace(config=cfg, metagraph=meta, uid=4, wallet=vw, oracle=c,
                          store=ValidatorStore(os.path.join(tmp, "t.db")), universe=fw.UniverseCache(c, timedelta(days=1)))
def refresh(now):
    n = cfg.neuron
    v.scores = scores_vector(v.store.rolling_scores(n.window_days, w.rolling_window_start(now, n.window_days, n.rolling_window_days)), meta.hotkeys)
v.refresh_scores = refresh

fw.query_round(v, thu)                                          # target window 2026-10-16 (1 day)
assert v.store.last_round_at() == thu
rows = v.store._conn.execute("SELECT DISTINCT target_window_start, window_days FROM predictions").fetchall()
assert rows == [("2026-10-16", 1)], rows
calls["floor"] = 0
fw.score_closed_predictions(v, datetime(2026, 10, 16, 23, 59, tzinfo=UTC))   # still open
assert calls["floor"] == 0
closed = datetime(2026, 10, 17, 0, 0, tzinfo=UTC)
fw.score_closed_predictions(v, closed)
assert calls["floor"] == 3, calls                              # one call per (ref, window)
fw.score_closed_predictions(v, closed)
assert calls["floor"] == 3                                      # nothing re-fetched / re-scored
refresh(closed)
print("scores:", v.scores)
assert v.scores == [1.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0], v.scores    # C excluded; dead + malformed = 0
refresh(closed + timedelta(days=40)); assert v.scores == [0.0] * 7  # outside rolling window
assert v.store._conn.execute("SELECT window_days FROM ground_truth").fetchall() == [(1,)] * 3

# --- set_weights + serve_axon intent wiring (fake subtensor)
from watch_oracle.base.validator import BaseValidatorNeuron
class FakeSub:
    def __init__(self): self.executed = []; self.block = 1000
    def execute(self, intent, wallet):
        self.executed.append(intent); return bt.ExtrinsicResult(success=True)
class V(BaseValidatorNeuron):
    def forward(self): pass
bv = object.__new__(V); bv.subtensor = FakeSub(); bv.wallet = vw; bv._last_weights_block = 0
bv.config = types.SimpleNamespace(netuid=7); bv.scores = [0.0, 0.0]
bv.set_weights(); assert bv.subtensor.executed == []          # all zero: skipped
bv.scores = [1.0, 0.0, 0.5]; bv.set_weights()
sw = bv.subtensor.executed[-1]
assert isinstance(sw, bt.SetWeights) and (sw.netuid, sw.uids, sw.weights, sw.version_key) == (7, [0, 2], [1.0, 0.5], V.spec_version)
assert bv._last_weights_block == 1000 and V.spec_version == 30

class M(BaseMinerNeuron):
    def forward(self, request): return request
bm = object.__new__(M); bm.subtensor = FakeSub(); bm.wallet = mw; bm.uid = 0
bm.config = types.SimpleNamespace(netuid=7, axon=types.SimpleNamespace(external_ip="1.2.3.4", external_port=8091))
bm.metagraph = types.SimpleNamespace(neuron=lambda uid: types.SimpleNamespace(axon="1.2.3.4:8091"))
bm.serve_axon(); assert bm.subtensor.executed == []           # already published
bm.metagraph = types.SimpleNamespace(neuron=lambda uid: types.SimpleNamespace(axon=None))
bm.serve_axon(); sa = bm.subtensor.executed[-1]
assert isinstance(sa, bt.ServeAxon) and (sa.netuid, sa.ip, sa.port) == (7, "1.2.3.4", 8091)

# --- config parsing + validation (no chain)
VA = ["--netuid", "7", "--subtensor.network", "test", "--neuron.window_days", "1", "--neuron.horizon_days", "1",
      "--neuron.error_band", "0.2", "--neuron.state_dir", tmp]
vc = BaseValidatorNeuron.config(VA); BaseValidatorNeuron.check_config(vc)
n = vc.neuron
assert (vc.netuid, n.window_days, n.horizon_days, n.error_band, n.batch_size, n.rolling_window_days) == (7, 1, 1, 0.2, 50, 28)
assert n.db_path.endswith("watch_oracle.db") and vc.subtensor.network == "test" and vc.logging.level == "INFO"
assert (BaseValidatorNeuron.config(["--neuron.state_dir", tmp]).neuron.window_days) == 7
for bad in (["--neuron.window_days", "0"], ["--neuron.window_days", "8"],
            ["--neuron.window_days", "7", "--neuron.rolling_window_days", "6"]):
    try: BaseValidatorNeuron.check_config(BaseValidatorNeuron.config(bad + ["--neuron.state_dir", tmp])); raise SystemExit(f"expected error {bad}")
    except ValueError: pass
MA = ["--miner.noise", "0.1", "--axon.external_ip", "1.2.3.4", "--neuron.state_dir", tmp]
mc = BaseMinerNeuron.config(MA); BaseMinerNeuron.check_config(mc)
assert (mc.miner.noise, mc.axon.port, mc.axon.external_port) == (0.1, 8091, 8091)
assert not hasattr(mc.miner, "window_days")
import contextlib, io
with contextlib.redirect_stderr(io.StringIO()):                # argparse's expected usage error
    try: BaseMinerNeuron.config(["--miner.window_days", "1"]); raise SystemExit("flag should be gone")
    except SystemExit as e: assert e.code == 2, e
for bad in (["--miner.noise", "1.5", "--axon.external_ip", "1.2.3.4"], []):
    try: BaseMinerNeuron.check_config(BaseMinerNeuron.config(bad + ["--neuron.state_dir", tmp])); raise SystemExit(f"expected error {bad}")
    except ValueError: pass

for s in (s_perfect, s_biased, s_bad): s.stop()
print("ALL SMOKE CHECKS PASSED")
