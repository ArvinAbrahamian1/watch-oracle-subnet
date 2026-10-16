"""
Signed HTTP transport, replacing the bittensor 9.x Axon/Dendrite stack.

Every request is signed with the validator's hotkey via bittensor.http_auth
(btauth/1) and bound to the receiving miner's hotkey. The miner verifies the
signature, freshness, receiver and nonce before doing any work.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Union

import bittensor as bt
import requests

from watch_oracle.protocol import ENDPOINT, MAX_BODY_BYTES, FloorPrediction, ProtocolError

logger = logging.getLogger(__name__)

MAX_CONCURRENT_QUERIES = 64
SERVER_SOCKET_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class MinerTarget:
    uid: int
    hotkey: str
    endpoint: str  # "ip:port" as served on chain


def query_miners(
    wallet: "bt.Wallet",
    targets: List[MinerTarget],
    request: FloorPrediction,
    timeout: float,
) -> Dict[int, Optional[FloorPrediction]]:
    """POST `request` to every target concurrently. A uid maps to None on any failure."""
    body = request.to_bytes()

    def query_one(target: MinerTarget) -> Optional[FloorPrediction]:
        # Signed per target, right before sending, so the nonce is fresh on arrival.
        headers = bt.http_auth.sign(
            wallet, method="POST", path=ENDPOINT, body=body, receiver_ss58=target.hotkey
        )
        headers["content-type"] = "application/json"
        try:
            resp = requests.post(
                f"http://{target.endpoint}{ENDPOINT}", data=body, headers=headers, timeout=timeout
            )
        except requests.RequestException as e:
            logger.debug("uid %s unreachable: %s", target.uid, type(e).__name__)
            return None
        if resp.status_code != 200:
            logger.debug("uid %s returned %s", target.uid, resp.status_code)
            return None
        if len(resp.content) > MAX_BODY_BYTES:
            logger.debug("uid %s response too large", target.uid)
            return None
        try:
            return FloorPrediction.from_bytes(resp.content)
        except ProtocolError as e:
            logger.debug("uid %s malformed response: %s", target.uid, e)
            return None

    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_QUERIES, len(targets))) as pool:
        return dict(zip((t.uid for t in targets), pool.map(query_one, targets)))


Handler = Callable[[FloorPrediction], FloorPrediction]
# Caller hotkey -> rejection reason, or None to accept.
Gate = Callable[[str], Optional[str]]


class MinerServer:
    """Threaded HTTP server for ENDPOINT. Verifies btauth/1, applies `gate`, then calls `handle`."""

    def __init__(self, wallet: "bt.Wallet", host: str, port: int, handle: Handler, gate: Gate):
        self._hotkey = wallet.hotkey.ss58_address
        self._handle = handle
        self._gate = gate
        self._nonces = bt.http_auth.InMemoryNonceStore()
        self._thread: Optional[threading.Thread] = None

        server = self

        class _RequestHandler(BaseHTTPRequestHandler):
            timeout = SERVER_SOCKET_TIMEOUT_SECONDS

            def do_POST(self):
                server._dispatch(self)

            def log_message(self, fmt, *args):
                logger.debug("%s %s", self.address_string(), fmt % args)

        self._httpd = ThreadingHTTPServer((host, port), _RequestHandler)
        self._httpd.daemon_threads = True

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        logger.info("Miner server listening on %s:%s%s", host, port, ENDPOINT)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _dispatch(self, req: BaseHTTPRequestHandler) -> None:
        if req.path != ENDPOINT:
            return _reply(req, 404, {"error": "not_found"})
        try:
            length = int(req.headers.get("content-length", ""))
        except ValueError:
            return _reply(req, 411, {"error": "length_required"})
        if length <= 0 or length > MAX_BODY_BYTES:
            return _reply(req, 413, {"error": "bad_body_size"})
        body = req.rfile.read(length)

        try:
            caller = bt.http_auth.verify(
                req.headers,
                body,
                method="POST",
                path=ENDPOINT,
                self_hotkey_ss58=self._hotkey,
                nonce_store=self._nonces,
            )
        except bt.http_auth.AuthError as e:
            return _reply(req, 401, {"error": type(e).__name__})

        reason = self._gate(caller.hotkey_ss58)
        if reason is not None:
            logger.debug("Rejected %s: %s", caller.hotkey_ss58, reason)
            return _reply(req, 403, {"error": reason})

        try:
            request = FloorPrediction.from_bytes(body)
        except ProtocolError as e:
            return _reply(req, 400, {"error": str(e)})

        try:
            response = self._handle(request)
        except Exception:
            logger.exception("Handler failed")
            return _reply(req, 500, {"error": "internal"})
        _reply(req, 200, response.to_bytes())


def _reply(req: BaseHTTPRequestHandler, status: int, payload: Union[bytes, dict]) -> None:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req.send_response(status)
    req.send_header("content-type", "application/json")
    req.send_header("content-length", str(len(body)))
    req.end_headers()
    req.wfile.write(body)
