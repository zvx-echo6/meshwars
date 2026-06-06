"""Async HTTP client for the upstream meshview API.

We do not assume a specific meshview version. The client is written to
accept multiple plausible response shapes (some forks expose decoded
position fields directly, some return raw bytes that need protobuf
decoding). When the latter is the case we fall back to manual decode.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Iterable

import httpx
from aiolimiter import AsyncLimiter

from .config import settings

log = logging.getLogger("meshview_client")


class MeshviewClient:
    def __init__(self):
        # Connection pool + global rate cap.
        self._limits = httpx.Limits(
            max_connections=settings.upstream_concurrency * 2,
            max_keepalive_connections=settings.upstream_concurrency,
        )
        self._client = httpx.AsyncClient(
            base_url=settings.meshview_url,
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=self._limits,
            headers={"Accept": "application/json", "User-Agent": "meshwars/1.0"},
        )
        self._rate = AsyncLimiter(settings.upstream_rate_per_sec, time_period=1.0)
        self._sem = asyncio.Semaphore(settings.upstream_concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with self._sem, self._rate:
            backoff = 1.0
            for attempt in range(5):
                try:
                    r = await self._client.get(path, params=params)
                    if r.status_code == 429 or r.status_code >= 500:
                        raise httpx.HTTPStatusError("upstream busy", request=r.request, response=r)
                    r.raise_for_status()
                    return r.json()
                except (httpx.HTTPError, httpx.TimeoutException) as e:
                    if attempt == 4:
                        log.warning("upstream %s failed after retries: %s", path, e)
                        raise
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    async def nodes(self, days_active: int = 30) -> list[dict]:
        """Roster snapshot. Tolerates list, {nodes:[...]}, or {data:[...]} envelopes."""
        data = await self._get("/api/nodes", {"days_active": days_active})
        return _unwrap_list(data, ("nodes", "data", "results"))

    async def stats(self) -> list[dict]:
        try:
            data = await self._get("/api/stats")
        except Exception:
            return []
        return _unwrap_list(data, ("stats", "data", "results"))

    async def packets(
        self,
        portnum: int,
        since_id: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        params: dict[str, Any] = {"portnum": portnum, "limit": limit}
        if since_id is not None:
            # Different meshview forks use different param names; send both.
            params["since"] = since_id
            params["after_id"] = since_id
        data = await self._get("/api/packets", params)
        return _unwrap_list(data, ("packets", "data", "results"))

    async def packets_seen(self, packet_id: int) -> list[dict]:
        try:
            data = await self._get(f"/api/packets_seen/{packet_id}")
        except Exception:
            return []
        return _unwrap_list(data, ("packets_seen", "seen", "data", "results"))


def _unwrap_list(data: Any, keys: Iterable[str]) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


# ----- position-packet helpers -----

def parse_meshtastic_payload_text(text: str) -> dict:
    """Parse meshview's text-format protobuf payload string.

    Format: "key: value\nkey: value\n..." where values can be ints,
    floats, or enum constants like LOC_MANUAL. We just need lat/lon ints.
    """
    out: dict = {}
    if not isinstance(text, str):
        return out
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        # Try int first, then float, otherwise keep string
        try:
            out[k] = int(v)
            continue
        except ValueError:
            pass
        try:
            out[k] = float(v)
            continue
        except ValueError:
            pass
        out[k] = v
    return out


def extract_position(packet: dict) -> tuple[float, float] | None:
    """Pull (lat, lon) in degrees out of a meshview packet record.

    Tries multiple shapes:
      1. packet has decoded fields: latitude_i / longitude_i (int * 1e7) or lat/lon.
      2. packet.payload_decoded contains lat/lon.
      3. packet has a nested 'position' object.
    Returns None if no usable position can be found.
    """
    # 1: top-level decoded ints
    lat_i = packet.get("latitude_i")
    lon_i = packet.get("longitude_i")
    if isinstance(lat_i, (int, float)) and isinstance(lon_i, (int, float)) and lat_i != 0:
        return (lat_i / 1e7, lon_i / 1e7)

    # 1b: top-level decoded floats
    lat = packet.get("latitude") or packet.get("lat")
    lon = packet.get("longitude") or packet.get("lon") or packet.get("long")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and lat != 0:
        # Heuristic: if magnitude looks like a 1e7-scaled int, scale down
        if abs(lat) > 1000:
            return (lat / 1e7, lon / 1e7)
        return (float(lat), float(lon))

    # 2: nested payload_decoded / decoded
    for k in ("payload_decoded", "decoded", "position"):
        inner = packet.get(k)
        if isinstance(inner, dict):
            res = extract_position(inner)
            if res is not None:
                return res

    # 3: meshview-style text-format protobuf string in `payload`
    payload_text = packet.get("payload")
    if isinstance(payload_text, str) and "latitude_i" in payload_text:
        parsed = parse_meshtastic_payload_text(payload_text)
        lat_i = parsed.get("latitude_i")
        lon_i = parsed.get("longitude_i")
        if isinstance(lat_i, (int, float)) and isinstance(lon_i, (int, float)) and lat_i != 0:
            return (lat_i / 1e7, lon_i / 1e7)

    return None


def extract_node_id(packet: dict) -> int | None:
    """Pull the sender node_id out of a packet record."""
    for k in ("from_node_id", "from", "from_id", "node_id"):
        v = packet.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            # Could be "!abcdef12" hex form
            s = v.lstrip("!")
            try:
                return int(s, 16) if any(c in "abcdefABCDEF" for c in s) else int(s)
            except ValueError:
                continue
    return None


def extract_timestamp(packet: dict) -> int:
    """Return packet timestamp in epoch seconds (best effort)."""
    # microseconds first (meshview's import_time_us)
    for k in ("import_time_us",):
        v = packet.get(k)
        if isinstance(v, (int, float)):
            return int(v / 1_000_000)
    for k in ("import_time", "rx_time", "timestamp", "time"):
        v = packet.get(k)
        if isinstance(v, (int, float)):
            # Heuristic: > 1e12 means ms
            return int(v / 1000) if v > 1e12 else int(v)
        if isinstance(v, str):
            try:
                from datetime import datetime
                return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
            except Exception:
                continue
    import time
    return int(time.time())


def extract_packet_id(packet: dict) -> int | None:
    for k in ("id", "packet_id"):
        v = packet.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                continue
    return None


def hop_count(seen_row: dict) -> int | None:
    """hops = hop_start - hop_limit; returns None if either is missing."""
    hs = seen_row.get("hop_start")
    hl = seen_row.get("hop_limit")
    if isinstance(hs, int) and isinstance(hl, int):
        return hs - hl
    return None


def is_via_mqtt(seen_row: dict) -> bool:
    """Detect MQTT-only receptions (no real RF reach).

    Meshview ALWAYS has a topic field because that's how it ingested the
    packet from MQTT. Topic presence does NOT mean the reception was
    MQTT-only — it means the gateway that heard the packet over RF
    republished it to MQTT for meshview to consume. We only reject if
    the upstream explicitly flags via_mqtt=true.
    """
    return seen_row.get("via_mqtt") is True


def extract_feeder_id(seen_row: dict) -> int | None:
    for k in ("node_id", "gateway_node_id", "rx_node_id", "rx_node", "gateway"):
        v = seen_row.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            s = v.lstrip("!")
            try:
                return int(s, 16) if any(c in "abcdefABCDEF" for c in s) else int(s)
            except ValueError:
                continue
    return None
