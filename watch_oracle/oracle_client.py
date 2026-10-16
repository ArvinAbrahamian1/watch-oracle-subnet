"""
HTTP client for the WatchComp ground truth oracle.

The API key is read from ORACLE_API_KEY and only ever sent in the x-oracle-key
request header. It is never logged, included in exceptions, or exposed via repr.
"""

import logging
import math
import os
import random
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

import requests

API_KEY_ENV = "ORACLE_API_KEY"
BASE_URL_ENV = "ORACLE_BASE_URL"
DEFAULT_BASE_URL = "https://www.watchcomp.ai"

logger = logging.getLogger(__name__)

MAX_TRIES = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0


class OracleError(Exception):
    """Base class for oracle failures."""


class OracleAuthError(OracleError):
    """Missing API key, or the oracle rejected it (401). Not retryable."""


class OracleUnavailableError(OracleError):
    """429, 5xx, or network failure that persisted for MAX_TRIES attempts."""


class OracleResponseError(OracleError):
    """Unexpected status code or malformed response body. Not retryable."""


@dataclass(frozen=True)
class UniverseRef:
    ref: str
    unique_listings: int


@dataclass(frozen=True)
class WindowFloor:
    ref: str
    window_start: date
    window_days: int
    floor_usd: float
    sample_count: int


class OracleClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
    ):
        key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")
        if not key:
            raise OracleAuthError(f"{API_KEY_ENV} is not set")
        self.base_url = (
            base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        ).rstrip("/")
        self._timeout = timeout
        self._headers = {"x-oracle-key": key, "accept": "application/json"}

    def __repr__(self) -> str:
        return f"OracleClient(base_url={self.base_url!r})"

    def get_universe(self) -> List[UniverseRef]:
        body = self._get("/api/oracle/universe")
        if body is None:
            raise OracleResponseError("/api/oracle/universe returned 404")
        try:
            return [
                UniverseRef(ref=str(r["ref"]), unique_listings=int(r["unique_listings"]))
                for r in body["refs"]
            ]
        except (KeyError, TypeError, ValueError) as e:
            raise OracleResponseError(f"malformed universe response: {e!r}") from None

    def get_floor(self, ref: str, window_start: date, window_days: int) -> Optional[WindowFloor]:
        """
        Floor for `ref` over the `window_days` window starting `window_start`,
        or None on 404 insufficient_data.
        """
        body = self._get(
            "/api/oracle/floor",
            params={"ref": ref, "start": window_start.isoformat(), "days": str(window_days)},
        )
        if body is None:
            return None
        try:
            floor = WindowFloor(
                ref=str(body["ref"]),
                window_start=date.fromisoformat(str(body["window_start"])),
                window_days=int(body["window_days"]),
                floor_usd=float(body["floor_usd"]),
                sample_count=int(body["sample_count"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise OracleResponseError(f"malformed floor response for {ref}: {e!r}") from None
        requested = (ref, window_start, window_days)
        if (floor.ref, floor.window_start, floor.window_days) != requested:
            raise OracleResponseError(f"floor response does not match request {requested}")
        if not math.isfinite(floor.floor_usd) or floor.floor_usd <= 0:
            raise OracleResponseError(f"non-positive floor_usd for {ref} {window_start}")
        return floor

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Optional[Any]:
        """GET `path` and return parsed JSON, or None on 404. Retries 429, 5xx and network errors."""
        url = self.base_url + path
        for attempt in range(1, MAX_TRIES + 1):
            retry_after: Optional[float] = None
            try:
                resp = requests.get(
                    url, params=params, headers=self._headers, timeout=self._timeout
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                reason = f"network error ({type(e).__name__})"
            else:
                status = resp.status_code
                if status == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        raise OracleResponseError(f"{path} returned non-JSON body") from None
                if status == 401:
                    raise OracleAuthError(
                        f"oracle rejected the API key (401) on {path}, check {API_KEY_ENV}"
                    )
                if status == 404:
                    logger.debug("Oracle %s 404 %s: %s", path, params, _error_field(resp))
                    return None
                if status == 429:
                    reason = "rate limited (429)"
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                elif 500 <= status < 600:
                    reason = f"server error ({status})"
                else:
                    raise OracleResponseError(f"unexpected status {status} from {path}")

            if attempt == MAX_TRIES:
                raise OracleUnavailableError(f"{path}: {reason} after {MAX_TRIES} tries")
            delay = (
                retry_after
                if retry_after is not None
                else BACKOFF_BASE_SECONDS * 2 ** (attempt - 1) + random.uniform(0, 0.5)
            )
            delay = min(delay, MAX_BACKOFF_SECONDS)
            logger.warning(
                "Oracle %s %s, retrying in %.1fs (attempt %s/%s)", path, reason, delay, attempt, MAX_TRIES
            )
            time.sleep(delay)
        raise AssertionError("unreachable")


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _error_field(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("error", "unknown"))
    except (ValueError, AttributeError):
        return "unknown"
