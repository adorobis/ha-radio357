"""Now-playing metadata for players (title, hosts, cover, duration).

Registered in ``hass.data["media_info_providers"]["radio357"]``. A player
integration that pushes URLs itself (for example to a DLNA renderer) can look
a media-source ID up there and send title, cover and duration along with the
URL. Home Assistant itself does not use this; it is an optional hook.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import unquote

import aiohttp
from homeassistant.core import HomeAssistant

from .api import Radio357Error
from .audio import async_size, image_path
from .const import DOMAIN, LIVE_STREAMS, LOGO, STATION

_LOGGER = logging.getLogger(__name__)

PREFIX = f"media-source://{DOMAIN}/"
PROVIDERS_KEY = "media_info_providers"


async def async_media_info(hass: HomeAssistant, media_content_id: str) -> dict[str, Any] | None:
    """Return metadata for one of our media-source IDs, None if not ours.

    Keys: title, artist, album, image (HA path or None), duration (s or
    None), size (bytes or None), mime, seekable.
    """
    if not media_content_id.startswith(PREFIX):
        return None
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    client = entries[0].runtime_data
    parts = [unquote(p) for p in media_content_id[len(PREFIX) :].split("/")]

    if len(parts) == 2 and parts[0] == "live" and parts[1] in LIVE_STREAMS:
        title, _, mime, _ = LIVE_STREAMS[parts[1]]
        artist = STATION
        try:
            now = await client.async_now_playing()
        except Radio357Error:
            now = {}
        if now.get("n"):
            # MusicCast Extras matches the speaker's track title against
            # this one, so keep it stable for the whole live session.
            artist = f"{now['n']} — {now['a']}" if now.get("a") else now["n"]
        return {
            "title": title,
            "artist": artist,
            "album": STATION,
            "image": image_path(LOGO),
            "duration": None,
            "size": None,
            "mime": mime,
            "seekable": False,
        }
    if len(parts) != 2 or parts[0] != "ep":
        return None
    try:
        episode = await client.async_episode(parts[1])
        size = await async_size(client, episode.id)
    except (Radio357Error, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("No metadata for %s: %s", media_content_id, err)
        return None
    album = episode.programmes[0][1] if episode.programmes else episode.title
    return {
        "title": episode.subtitle or episode.title,
        "artist": episode.hosts or album,
        "album": album,
        "image": image_path(episode.image),
        "duration": episode.duration,
        "size": size,
        "mime": "audio/mpeg",
        "seekable": True,
    }


def register(hass: HomeAssistant) -> None:
    """Make the provider available to player integrations."""
    hass.data.setdefault(PROVIDERS_KEY, {})[DOMAIN] = async_media_info
