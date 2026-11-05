"""Opt-in, HMAC-authenticated disk inbox; never starts an agent.

The operator selects an absolute events root and an allowlist of sources in
webhook settings. Keys are captured in the adapter's owning profile scope.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from gateway.platforms._shared import get_scoped_secret

logger = logging.getLogger(__name__)
_SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SEEN_CAP = 512


def _publish(path: Path, value: dict) -> None:
    """Consumers see complete files only; an I/O error stays retryable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_seen(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        ids = value.get("ids") if isinstance(value, dict) else None
        if isinstance(ids, list):
            return [item for item in ids if isinstance(item, str)][-_SEEN_CAP:]
    except (OSError, ValueError):
        pass
    # A duplicate is recoverable by the consumer; rejecting a real event is not.
    return []


class CronEventReceiver:
    """A receiver belongs to the adapter that constructs it, not a request scope."""

    def __init__(self, root: Path, sources: list[str], max_body_bytes: int):
        self.root = root
        self.max_body_bytes = max_body_bytes
        shared = get_scoped_secret("CRON_EVENTS_HMAC_SECRET") or ""
        self.keys = {}
        for source in sources:
            name = f"CRON_EVENTS_SECRET__{source.upper().replace('-', '_')}"
            key = get_scoped_secret(name) or shared
            self.keys[source] = key if isinstance(key, str) and key.strip() else ""

    @classmethod
    def from_config(cls, extra: dict, max_body_bytes: int) -> CronEventReceiver | None:
        root = extra.get("cron_events_root")
        if root is None or root == "":
            return None
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise ValueError("cron_events_root must be an absolute directory path")
        sources = extra.get("cron_events_sources", ["github"])
        if not isinstance(sources, list) or not sources or any(
            not isinstance(source, str) or not _SOURCE_RE.fullmatch(source) for source in sources
        ):
            raise ValueError("cron_events_sources must be a nonempty list of safe source names")
        return cls(Path(root), sources, max_body_bytes)

    async def handle(self, request: web.Request) -> web.Response:
        source = request.match_info["source"]
        if source not in self.keys:
            return web.Response(status=404)
        if request.content_length is not None and request.content_length > self.max_body_bytes:
            return web.Response(status=413)
        key = self.keys[source]
        if not key:
            return web.Response(status=503)
        raw = await request.read()  # aiohttp also bounds chunked bodies (HTTP 413).
        if len(raw) > self.max_body_bytes:
            return web.Response(status=413)
        expected = "sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            return web.Response(status=401)
        return self._persist(source, request.headers, raw)

    def _persist(self, source: str, headers, raw: bytes) -> web.Response:
        delivery = (headers.get("X-GitHub-Delivery") or headers.get("X-Request-Id") or "").strip()
        if len(delivery) > 256:
            return web.Response(status=400)
        seen_path = self.root / "state" / f"seen-deliveries-{source}.json"
        seen = _load_seen(seen_path) if delivery else []
        if delivery and delivery in seen:
            return web.Response(status=204)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = {"_raw_b64": base64.b64encode(raw).decode("ascii")}
        received = datetime.now(UTC)
        envelope = {
            "schema": 1, "source": source, "received_at": received.isoformat(),
            "delivery_id": delivery, "event": headers.get("X-GitHub-Event", "unknown"),
            "headers": {"user-agent": headers.get("User-Agent", ""),
                        "content-type": headers.get("Content-Type", "")},
            "payload": payload,
        }
        filename = f"{received:%Y%m%dT%H%M%S}.{received.microsecond:06d}Z-{uuid.uuid4().hex[:8]}.json"
        try:
            _publish(self.root / "inbox" / source / filename, envelope)
        except OSError:
            logger.error("[cron-events] inbox publication failed source=%s", source)
            return web.Response(status=500)
        if delivery:
            try:
                _publish(seen_path, {"ids": (seen + [delivery])[-_SEEN_CAP:]})
            except OSError:
                logger.warning("[cron-events] dedupe degraded source=%s", source)
        return web.Response(status=204)
