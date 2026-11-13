"""Audio proxy: streams episodes and live radio through Home Assistant.

Players (MusicCast's DLNA renderer in particular) get a plain LAN URL and
don't have to follow the chain of HTTPS redirects (gateway → Spreaker →
CloudFront, or stream.radio357.pl → revma). The URL is signed by Home
Assistant when a media player plays it, so ``requires_auth`` holds.

Episodes are served with Content-Length and byte ranges (seeking). If the
upstream drops the connection, the proxy reconnects at the next missing
byte, fetching a new signed URL when the old one has expired.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .api import AccessError, Radio357Error
from .const import AUDIO_VIEW_URL, DOMAIN, IMAGE_VIEW_URL, LIVE_STREAMS, STREAM_URL

if TYPE_CHECKING:
    from .api import Radio357Client

_LOGGER = logging.getLogger(__name__)

# sock_read well below the ~100 s speaker buffer, so a stalled download is
# retried before the speaker runs dry.
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=15, sock_read=20)
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=20)
SIZE_TTL = 6 * 3600
CHUNK = 64 * 1024
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")
_ID = re.compile(r"^\d+$")
MAX_RETRIES = 6
RETRY_RESET_BYTES = 2_000_000  # this much streamed fine resets the retry count
UPSTREAM_ERRORS = (
    aiohttp.ClientPayloadError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientConnectorError,
    aiohttp.ServerTimeoutError,
    aiohttp.ClientOSError,
    TimeoutError,
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _unb64(key: str) -> str:
    try:
        return base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as err:
        raise web.HTTPNotFound from err


def audio_path(kind: str, key: str) -> str:
    """Return the proxy path: kind "ep" (episode id) or "live" (stream key)."""
    if kind == "live":
        ext = "aac" if LIVE_STREAMS[key][2] == "audio/aac" else "mp3"
        return f"{AUDIO_VIEW_URL}/live/{key}.{ext}"
    return f"{AUDIO_VIEW_URL}/ep/{key}.mp3"


def live_url(key: str) -> str:
    """Return the stream.radio357.pl URL of a live variant."""
    return f"{STREAM_URL}?s={LIVE_STREAMS[key][1]}"


MAX_REDIRECTS = 8
REDIRECTS = (301, 302, 303, 307, 308)


@contextlib.asynccontextmanager
async def _get(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    client_timeout: aiohttp.ClientTimeout,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """GET following redirects without re-encoding the URLs.

    The audio is behind Spreaker → CloudFront redirects whose signed query
    strings must reach the server byte for byte; aiohttp's own redirect
    handling normalises the escapes and CloudFront then answers 403.
    """
    for _ in range(MAX_REDIRECTS):
        async with session.get(
            URL(url, encoded=True),
            headers=headers,
            timeout=client_timeout,
            allow_redirects=False,
        ) as resp:
            location = resp.headers.get("Location")
            if resp.status in REDIRECTS and location:
                url = str(resp.url.join(URL(location, encoded=True)))
                continue
            yield resp
            return
    raise Radio357Error("Too many redirects")


_SIZES: dict[str, tuple[float, int]] = {}


class _ClientGone(Exception):
    """The player closed its connection."""


class _UpstreamFailed(Exception):
    """The audio host kept failing."""


async def async_size(client: Radio357Client, episode_id: str) -> int:
    """Return the size of an episode's file in bytes.

    Raises AccessError, Radio357Error or aiohttp errors.
    """
    cached = _SIZES.get(episode_id)
    if cached and time.monotonic() - cached[0] < SIZE_TTL:
        return cached[1]
    for fresh in (False, True):
        url = await client.async_audio_url(episode_id, fresh=fresh)
        headers = {"User-Agent": client.headers["User-Agent"], "Range": "bytes=0-0"}
        async with _get(client.session, url, headers, PROBE_TIMEOUT) as resp:
            if resp.status in (401, 403, 404, 410) and not fresh:
                continue  # the signed URL expired
            if resp.status >= 400:
                raise Radio357Error(f"Audio host: HTTP {resp.status}")
            total = resp.headers.get("Content-Range", "").rpartition("/")[2]
            if not total.isdigit():
                total = resp.headers.get("Content-Length", "") if resp.status == 200 else ""
            if not total.isdigit():
                raise Radio357Error("Audio size unknown")
            _SIZES[episode_id] = (time.monotonic(), int(total))
            return int(total)
    raise Radio357Error("Audio host refused the URL")


async def _write(response: web.StreamResponse, chunk: bytes) -> None:
    try:
        await response.write(chunk)
    except (ConnectionResetError, aiohttp.ClientConnectionResetError, RuntimeError) as err:
        raise _ClientGone from err


async def _pipe_episode(
    client: Radio357Client, response: web.StreamResponse, episode_id: str, lo: int, hi: int
) -> None:
    """Copy bytes lo..hi of an episode, reconnecting where it broke."""
    pos, failures, since_failure, fresh = lo, 0, 0, False
    while pos <= hi:
        try:
            url = await client.async_audio_url(episode_id, fresh=fresh)
            fresh = False
            headers = {"User-Agent": client.headers["User-Agent"], "Range": f"bytes={pos}-{hi}"}
            async with _get(client.session, url, headers, STREAM_TIMEOUT) as resp:
                if resp.status in (401, 403, 410):
                    fresh = True
                    raise _UpstreamFailed(f"HTTP {resp.status} (expired URL?)")
                if resp.status >= 400:
                    raise _UpstreamFailed(f"HTTP {resp.status}")
                if resp.status == 200 and pos > 0:
                    raise _UpstreamFailed("audio host ignored the byte range")
                async for chunk in resp.content.iter_chunked(CHUNK):
                    await _write(response, chunk[: hi - pos + 1])
                    pos += len(chunk)
                    since_failure += len(chunk)
                    if since_failure > RETRY_RESET_BYTES:
                        failures = 0
                    if pos > hi:
                        break
            if pos <= hi:
                raise _UpstreamFailed(f"short body, {hi - pos + 1} bytes missing")
        except (_UpstreamFailed, Radio357Error, *UPSTREAM_ERRORS) as err:
            if isinstance(err, AccessError):
                raise _UpstreamFailed(str(err)) from err
            failures += 1
            since_failure = 0
            if failures > MAX_RETRIES:
                raise _UpstreamFailed(f"{err!r} after {MAX_RETRIES} retries") from err
            _LOGGER.info(
                "Upstream dropped episode %s at byte %s (%r), reconnecting (%s)",
                episode_id,
                pos,
                err,
                failures,
            )
            await asyncio.sleep(min(2 * failures, 10))


class Radio357AudioView(HomeAssistantView):
    """Stream Radio 357 audio with the logged-in account."""

    url = AUDIO_VIEW_URL + "/{kind}/{name}"
    name = "api:radio357:audio"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    def _client(self) -> Radio357Client:
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise web.HTTPServiceUnavailable(text="Radio 357 is not set up")
        return entries[0].runtime_data

    @staticmethod
    def _range(request: web.Request, total: int) -> tuple[int, int] | None:
        """Return the requested inclusive byte range, or None for all."""
        match = _RANGE.match(request.headers.get("Range", "").strip())
        if not match or not (match.group(1) or match.group(2)):
            return None
        if not match.group(1):  # suffix range: last N bytes
            first, last = max(0, total - int(match.group(2))), total - 1
        else:
            first = int(match.group(1))
            last = min(int(match.group(2)), total - 1) if match.group(2) else total - 1
        if first >= total or first > last:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{total}"})
        return first, last

    async def _serve_episode(
        self, request: web.Request, name: str, *, body: bool
    ) -> web.StreamResponse:
        episode_id = name.removesuffix(".mp3")
        if not _ID.match(episode_id):
            raise web.HTTPNotFound
        client = self._client()
        try:
            total = await async_size(client, episode_id)
        except AccessError as err:
            raise web.HTTPForbidden(text=str(err)) from err
        except (Radio357Error, aiohttp.ClientError, TimeoutError) as err:
            raise web.HTTPBadGateway(text=str(err)) from err
        wanted = self._range(request, total)
        first, last = wanted or (0, total - 1)
        response = web.StreamResponse(status=206 if wanted else 200)
        response.headers["Content-Type"] = "audio/mpeg"
        response.headers["Accept-Ranges"] = "bytes"
        response.content_length = last - first + 1
        if wanted:
            response.headers["Content-Range"] = f"bytes {first}-{last}/{total}"
        await response.prepare(request)
        if not body:
            return response
        try:
            await _pipe_episode(client, response, episode_id, first, last)
        except _ClientGone:
            pass  # players drop the connection to seek or stop; that's normal
        except _UpstreamFailed as err:
            # The player sees a short body; it will request the rest again.
            _LOGGER.warning("Giving up on episode %s: %s", episode_id, err)
        return response

    async def _serve_live(self, request: web.Request, name: str) -> web.StreamResponse:
        key = name.rsplit(".", 1)[0]
        if key not in LIVE_STREAMS:
            raise web.HTTPNotFound
        client = self._client()
        mime = LIVE_STREAMS[key][2]
        response = web.StreamResponse()
        response.headers["Content-Type"] = mime
        await response.prepare(request)
        failures = 0
        # Reconnect for as long as the player listens: MP3/AAC decoders
        # resync on the next frame, so a reconnect is just a short gap. The
        # revma URL behind the redirect is short-lived, so always start over.
        while failures <= MAX_RETRIES:
            try:
                async with client.session.get(
                    live_url(key),
                    headers={"User-Agent": client.headers["User-Agent"], "Icy-MetaData": "0"},
                    timeout=STREAM_TIMEOUT,
                ) as resp:
                    if resp.status >= 400:
                        raise _UpstreamFailed(f"HTTP {resp.status}")
                    received = 0
                    async for chunk in resp.content.iter_chunked(CHUNK):
                        await _write(response, chunk)
                        received += len(chunk)
                        if received > RETRY_RESET_BYTES:
                            failures, received = 0, 0
                raise _UpstreamFailed("stream ended")
            except _ClientGone:
                return response
            except (_UpstreamFailed, *UPSTREAM_ERRORS) as err:
                failures += 1
                _LOGGER.info("Live stream %s dropped (%r), reconnecting (%s)", key, err, failures)
                await asyncio.sleep(min(2 * failures, 10))
        _LOGGER.warning("Live stream %s: giving up after %s reconnects", key, MAX_RETRIES)
        return response

    async def head(self, request: web.Request, kind: str, name: str) -> web.StreamResponse:
        """Answer HEAD probes from renderers without streaming the body."""
        if kind == "live":
            key = name.rsplit(".", 1)[0]
            if key not in LIVE_STREAMS:
                raise web.HTTPNotFound
            return web.Response(headers={"Content-Type": LIVE_STREAMS[key][2]})
        if kind != "ep":
            raise web.HTTPNotFound
        return await self._serve_episode(request, name, body=False)

    async def get(self, request: web.Request, kind: str, name: str) -> web.StreamResponse:
        """Stream audio, honouring Range for seeking."""
        if kind == "live":
            return await self._serve_live(request, name)
        if kind != "ep":
            raise web.HTTPNotFound
        return await self._serve_episode(request, name, body=True)


IMAGE_HOSTS = ("media.radio357.pl", "s3media.radio357.pl", "radio357.pl")
# Speakers want a plain, small JPEG; media.radio357.pl resizes on the fly.
IMAGE_PARAMS = {"media.radio357.pl": "w=500&h=500&fit=crop&fm=jpg"}


def _image_allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in IMAGE_HOSTS and ".." not in parsed.path


def image_path(url: str | None) -> str | None:
    """Return the proxy path for a Radio 357 image, else None."""
    if not url or not _image_allowed(url):
        return None
    return f"{IMAGE_VIEW_URL}/{_b64(url)}.jpg"


class Radio357ImageView(HomeAssistantView):
    """Serve the station's cover images over the LAN for speakers.

    Speakers fetch album art without Home Assistant credentials, so this view
    is public. It only relays public images from Radio 357's media hosts.
    """

    url = IMAGE_VIEW_URL + "/{name}"
    name = "api:radio357:image"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request, name: str) -> web.Response:
        """Relay one image."""
        url = _unb64(name.rsplit(".", 1)[0])
        if not _image_allowed(url):
            raise web.HTTPNotFound
        extra = IMAGE_PARAMS.get(urlparse(url).hostname or "")
        if extra:
            url += ("&" if "?" in url else "?") + extra
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url, headers={"Accept": "image/jpeg,image/*"}, timeout=PROBE_TIMEOUT
            ) as resp:
                if resp.status != 200 or not resp.content_type.startswith("image/"):
                    raise web.HTTPNotFound
                body = await resp.read()
                return web.Response(
                    body=body,
                    content_type=resp.content_type,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise web.HTTPBadGateway(text=str(err)) from err
