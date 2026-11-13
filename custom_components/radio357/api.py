"""Client for the Radio 357 JSON API (the one radio357.pl and the apps use).

- ``auth.r357.eu/api``: login (e-mail + password) returns an access and a
  refresh token; ``/account`` tells whether the account is a patron.
- ``static.radio357.pl/api/content/v1``: public catalogue (programmes,
  episodes with duration, cover and the access level they need).
- ``gateway.r357.eu/api``: per-user calls with the access token, e.g. the
  audio URL of an episode (403/401 when the account may not play it) and
  the favourite programmes ("likes").
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Any

import aiohttp

from .const import NOW_PLAYING_URL

_LOGGER = logging.getLogger(__name__)

AUTH_URL = "https://auth.r357.eu/api"
GATEWAY_URL = "https://gateway.r357.eu/api"
CONTENT_URL = "https://static.radio357.pl/api/content/v1"

USER_AGENT = "HomeAssistant-Radio357/1.0"
TIMEOUT = aiohttp.ClientTimeout(total=30)
PROGRAMMES_TTL = 12 * 3600
PAGE_TTL = 600
AUDIO_URL_TTL = 3600  # the signed Spreaker/CloudFront URLs last about a day
NOW_TTL = 20

# Access levels an episode can require, lowest first. Unknown values are
# treated as playable; the gateway has the final word anyway.
LEVELS = ("anonymous", "user", "patron")


class Radio357Error(Exception):
    """Talking to the API failed."""


class AuthError(Radio357Error):
    """The e-mail or password was rejected."""


class AccessError(Radio357Error):
    """The account may not play this episode."""


@dataclass(slots=True)
class Programme:
    """A programme (audycja)."""

    id: str
    slug: str
    name: str
    image: str | None
    hosts: str
    exclusive: bool


@dataclass(slots=True)
class Episode:
    """One podcast episode."""

    id: str
    title: str
    subtitle: str
    image: str | None
    duration: int | None
    published: str
    access: str
    hosts: str
    programmes: list[tuple[str, str]] = field(default_factory=list)  # (slug, name)

    @property
    def name(self) -> str:
        """Title for lists: the episode subtitle with the programme name."""
        if self.subtitle and self.subtitle != self.title:
            return f"{self.title} — {self.subtitle}"
        return self.title


@dataclass(slots=True)
class EpisodePage:
    """One page of the API (page is 0-based)."""

    episodes: list[Episode]
    page: int
    page_size: int
    total: int


def _hosts(team: Any) -> str:
    if not isinstance(team, list):
        return ""
    return ", ".join(t["name"] for t in team if isinstance(t, dict) and t.get("name"))


def _episode(raw: dict[str, Any]) -> Episode:
    duration = raw.get("duration")
    return Episode(
        id=str(raw["id"]),
        title=(raw.get("title") or "").strip(),
        subtitle=(raw.get("subTitle") or "").strip(),
        image=raw.get("image") or None,
        duration=int(duration) if isinstance(duration, int | float) and duration > 0 else None,
        published=raw.get("published") or "",
        access=raw.get("access") or "anonymous",
        hosts=_hosts(raw.get("team")),
        programmes=[
            (p["slug"], p.get("name") or p["slug"])
            for p in raw.get("programs") or []
            if isinstance(p, dict) and p.get("slug")
        ],
    )


def _programme(raw: dict[str, Any]) -> Programme:
    return Programme(
        id=str(raw["id"]),
        slug=raw["slug"],
        name=(raw.get("name") or raw["slug"]).strip(),
        image=raw.get("image") or None,
        hosts=_hosts(raw.get("team")),
        exclusive=bool(raw.get("isExclusive")),
    )


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message") or body.get("errorMessage") or "")
        return str(body.get("errorMessage") or body.get("message") or "")
    return ""


class Radio357Client:
    """Logged-in access to the Radio 357 API."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        """Initialize the client."""
        self.session = session
        self._email = email
        self._password = password
        self._access: str | None = None
        self._refresh: str | None = None
        self._auth_lock = asyncio.Lock()
        self.level = "anonymous"
        self.account: dict[str, Any] = {}
        self._programmes: tuple[float, list[Programme]] | None = None
        self._pages: dict[str, tuple[float, EpisodePage]] = {}
        self._episodes: dict[str, Episode] = {}
        self._audio_urls: dict[str, tuple[float, str]] = {}
        self._now: tuple[float, dict[str, Any]] | None = None
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    # ---------------------------------------------------------------- auth

    async def _json(
        self, method: str, url: str, *, token: str | None = None, **kwargs: Any
    ) -> tuple[int, Any]:
        headers = dict(self.headers)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self.session.request(
                method, url, headers=headers, timeout=TIMEOUT, **kwargs
            ) as resp:
                text = await resp.text()
                try:
                    body = await resp.json(content_type=None) if text else None
                except ValueError:
                    body = None
                return resp.status, body
        except (aiohttp.ClientError, TimeoutError) as err:
            raise Radio357Error(f"{url}: {err!r}") from err

    async def async_login(self) -> None:
        """Log in and read the account (patron or not)."""
        async with self._auth_lock:
            await self._login()
        await self._load_account()

    async def _login(self) -> None:
        credentials = {"email": self._email, "password": self._password}
        status, body = await self._json("POST", f"{AUTH_URL}/auth/login", json=credentials)
        if status in (400, 401, 403, 422):
            raise AuthError(_error_message(body) or f"HTTP {status}")
        if status != 200 or not isinstance(body, dict) or not body.get("accessToken"):
            raise Radio357Error(f"Login failed: HTTP {status}")
        self._access, self._refresh = body["accessToken"], body.get("refreshToken")

    async def _renew(self, stale: str | None) -> None:
        """Get a new access token (refresh, else log in again)."""
        async with self._auth_lock:
            if self._access != stale:
                return  # another request already renewed it
            if self._refresh:
                status, body = await self._json(
                    "POST", f"{AUTH_URL}/auth/refresh", json={"refreshToken": self._refresh}
                )
                if status == 200 and isinstance(body, dict) and body.get("accessToken"):
                    self._access = body["accessToken"]
                    self._refresh = body.get("refreshToken") or self._refresh
                    return
                _LOGGER.debug("Token refresh failed (HTTP %s), logging in again", status)
            await self._login()

    async def _authed(self, method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
        if not self._access:
            await self._renew(None)
        token = self._access
        status, body = await self._json(method, url, token=token, **kwargs)
        if status == 401:  # expired token; a real "no access" stays 401/403 after
            await self._renew(token)
            status, body = await self._json(method, url, token=self._access, **kwargs)
        return status, body

    async def _load_account(self) -> None:
        status, body = await self._authed("GET", f"{AUTH_URL}/account")
        if status != 200 or not isinstance(body, dict):
            raise Radio357Error(f"Account: HTTP {status}")
        self.account = body
        patron = body.get("patronIdHash")
        self.level = "patron" if isinstance(patron, str) and len(patron) > 10 else "user"
        _LOGGER.debug("Logged in as %s account", self.level)

    def can_play(self, episode: Episode) -> bool:
        """Return whether the account's level covers the episode."""
        if episode.access not in LEVELS:
            return True
        return LEVELS.index(episode.access) <= LEVELS.index(self.level)

    @property
    def is_patron(self) -> bool:
        """Return True for patron accounts."""
        return self.level == "patron"

    # ------------------------------------------------------------ catalogue

    async def _content(self, path: str) -> Any:
        status, body = await self._json("GET", f"{CONTENT_URL}/{path}")
        if status != 200 or body is None:
            raise Radio357Error(f"{path}: HTTP {status}")
        return body

    async def async_programmes(self) -> list[Programme]:
        """Return all programmes, sorted by name."""
        if self._programmes and time.monotonic() - self._programmes[0] < PROGRAMMES_TTL:
            return self._programmes[1]
        programmes: list[Programme] = []
        page = 0
        while True:
            body = await self._content(f"programs/?page={page}")
            raw = (body.get("_embedded") or {}).get("programs") or []
            programmes.extend(_programme(p) for p in raw if p.get("slug"))
            if not raw or "next" not in (body.get("_links") or {}):
                break
            page += 1
        programmes.sort(key=lambda p: p.name.casefold())
        self._programmes = (time.monotonic(), programmes)
        return programmes

    async def async_programme(self, slug: str) -> Programme | None:
        """Return one programme by slug."""
        return next((p for p in await self.async_programmes() if p.slug == slug), None)

    async def _episode_page(self, path: str, page: int) -> EpisodePage:
        key = f"{path}?page={page}"
        cached = self._pages.get(key)
        if cached and time.monotonic() - cached[0] < PAGE_TTL:
            return cached[1]
        body = await self._content(key)
        episodes = [_episode(e) for e in (body.get("_embedded") or {}).get("podcasts") or []]
        for ep in episodes:
            self._episodes[ep.id] = ep
        result = EpisodePage(
            episodes=episodes,
            page=int(body.get("page") or page),
            page_size=int(body.get("page_size") or len(episodes) or 1),
            total=int(body.get("total") or len(episodes)),
        )
        self._pages[key] = (time.monotonic(), result)
        return result

    async def async_latest(self, page: int = 0) -> EpisodePage:
        """Return the newest episodes of all programmes (API page)."""
        return await self._episode_page("podcasts", page)

    async def async_programme_episodes(self, slug: str, page: int = 0) -> EpisodePage:
        """Return a programme's episodes, newest first (API page)."""
        return await self._episode_page(f"programs/{slug}/podcasts", page)

    async def async_episode(self, episode_id: str) -> Episode:
        """Return one episode."""
        if episode_id in self._episodes:
            return self._episodes[episode_id]
        body = await self._content(f"podcasts/{episode_id}")
        if not isinstance(body, dict) or "id" not in body:
            raise Radio357Error(f"No episode {episode_id}")
        episode = _episode(body)
        self._episodes[episode.id] = episode
        return episode

    # ------------------------------------------------------------- per user

    async def async_audio_url(self, episode_id: str, *, fresh: bool = False) -> str:
        """Return the (signed) audio URL; AccessError if not allowed."""
        cached = self._audio_urls.get(episode_id)
        if not fresh and cached and time.monotonic() - cached[0] < AUDIO_URL_TTL:
            return cached[1]
        status, body = await self._authed("GET", f"{GATEWAY_URL}/content/podcast/{episode_id}/url")
        if status in (401, 403):
            raise AccessError(_error_message(body) or f"HTTP {status}")
        if status != 200 or not isinstance(body, dict) or not body.get("url"):
            raise Radio357Error(f"Audio URL of {episode_id}: HTTP {status}")
        self._audio_urls[episode_id] = (time.monotonic(), body["url"])
        return body["url"]

    async def async_favourite_programmes(self) -> list[Programme]:
        """Return the programmes the account marked as favourite."""
        status, body = await self._authed("GET", f"{GATEWAY_URL}/likes?resource=Program")
        if status in (401, 403):  # the website asks non-patrons to become one
            raise AccessError(_error_message(body) or f"HTTP {status}")
        if status != 200:
            raise Radio357Error(f"Favourites: HTTP {status}")
        ids = [str(i) for i in body or [] if isinstance(i, str | int)]
        by_id = {p.id: p for p in await self.async_programmes()}
        return [by_id[i] for i in ids if i in by_id]

    async def async_now_playing(self) -> dict[str, Any]:
        """Return what the live stream is playing (playing.json "p" record)."""
        if self._now and time.monotonic() - self._now[0] < NOW_TTL:
            return self._now[1]
        status, body = await self._json("GET", NOW_PLAYING_URL)
        now = body.get("p") if status == 200 and isinstance(body, dict) else None
        self._now = (time.monotonic(), now if isinstance(now, dict) else {})
        return self._now[1]
