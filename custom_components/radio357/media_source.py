"""Media source for Radio 357: live radio, programmes and podcasts.

Identifiers (after ``media-source://radio357/``), segments URL-quoted:
    live, live/<key>          live variants
    favs                      favourite programmes of the account
    latest/<page>             newest episodes of all programmes
    progs, progs/<letter>     programmes A-Z
    prog/<slug>/<page>        episodes of a programme, newest first
    ep/<id>                   one episode
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from urllib.parse import quote, unquote

from homeassistant.components.media_player import (
    BrowseError,
    MediaClass,
    MediaType,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .api import AccessError, Episode, EpisodePage, Programme, Radio357Client, Radio357Error
from .audio import audio_path
from .const import DOMAIN, LIVE_STREAMS, LOGO, STATION

# Players such as MusicCast hide playable items whose content type isn't
# an audio/* MIME type, so every playable item carries one.
AUDIO_MPEG = "audio/mpeg"
PAGE_SIZE = 50
SEARCH_PAGES = 4  # API pages of newest episodes searched (250 each)
LOCK = "🔒 "
_HAS_DATE = re.compile(r"\b\d{1,2}\.\d{1,2}\.(\d{2}|\d{4})\b")
LEVEL_NAMES = {"user": "zalogowanych", "patron": "patronów"}


async def async_get_media_source(hass: HomeAssistant) -> Radio357MediaSource:
    """Set up the media source."""
    return Radio357MediaSource(hass)


def _ident(*parts: str | int) -> str:
    return "/".join(quote(str(p), safe="") for p in parts)


def _folder(ident: str, title: str, thumbnail: str | None = None) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=ident,
        media_class=MediaClass.DIRECTORY,
        media_content_type=MediaType.PLAYLIST,
        title=title,
        can_play=False,
        can_expand=True,
        thumbnail=thumbnail,
    )


def _programme(prog: Programme) -> BrowseMediaSource:
    folder = _folder(_ident("prog", prog.slug, 1), prog.name, prog.image)
    folder.media_class = MediaClass.PODCAST
    return folder


def _date(published: str) -> str:
    """'2026-11-13T18:40:00+01:00' -> '13.11.2026'."""
    day = published[:10].split("-")
    return ".".join(reversed(day)) if len(day) == 3 else ""


def _letter(name: str) -> str:
    """Return the A-Z group of a programme name (diacritics folded)."""
    for char in name:
        if char.isalnum():
            if char.isdigit():
                return "0-9"
            base = unicodedata.normalize("NFKD", char.replace("ł", "l").replace("Ł", "L"))
            return base[0].upper()
    return "#"


class Radio357MediaSource(MediaSource):
    """Browse and play Radio 357."""

    name = STATION

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the source."""
        super().__init__(DOMAIN)
        self.hass = hass

    def _client(self) -> Radio357Client:
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise BrowseError("Radio 357 is not set up")
        return entries[0].runtime_data

    def _episode(
        self, client: Radio357Client, ep: Episode, *, programme: bool
    ) -> BrowseMediaSource:
        title = ep.subtitle or ep.title
        if programme and ep.subtitle and ep.title not in ep.subtitle:
            title = ep.name
        if (day := _date(ep.published)) and not _HAS_DATE.search(title):
            title = f"{title} ({day})"
        playable = client.can_play(ep)
        if not playable:
            title = f"{LOCK}{title} — tylko dla {LEVEL_NAMES.get(ep.access, ep.access)}"
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=_ident("ep", ep.id),
            media_class=MediaClass.PODCAST,
            media_content_type=AUDIO_MPEG,
            title=title,
            can_play=playable,
            can_expand=False,
            thumbnail=ep.image,
        )

    # ------------------------------------------------------------- playing

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Return the proxied audio URL."""
        parts = [unquote(p) for p in (item.identifier or "").split("/")]
        if len(parts) == 2 and parts[0] == "live" and parts[1] in LIVE_STREAMS:
            return PlayMedia(audio_path("live", parts[1]), LIVE_STREAMS[parts[1]][2])
        if len(parts) == 2 and parts[0] == "ep" and parts[1].isdigit():
            try:
                # Fail here, with a readable message, rather than in the proxy.
                await self._client().async_audio_url(parts[1])
            except AccessError as err:
                raise Unresolvable(f"Radio 357: brak dostępu ({err})") from err
            except (Radio357Error, BrowseError) as err:
                raise Unresolvable(str(err)) from err
            return PlayMedia(audio_path("ep", parts[1]), AUDIO_MPEG)
        raise Unresolvable(f"Not playable: {item.identifier}")

    # ------------------------------------------------------------ browsing

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Return a folder."""
        parts = [unquote(p) for p in item.identifier.split("/")] if item.identifier else []
        try:
            return await self._browse(self._client(), parts)
        except Radio357Error as err:
            raise BrowseError(str(err)) from err

    async def _browse(self, client: Radio357Client, parts: list[str]) -> BrowseMediaSource:
        kind, args = (parts[0], parts[1:]) if parts else ("", [])
        if not kind:
            return self._root()
        if kind == "live":
            return self._live(client)
        if kind == "favs":
            folder = _folder("favs", "Ulubione audycje")
            try:
                favourites = await client.async_favourite_programmes()
            except AccessError:
                folder.children = [_folder("progs", "(ulubione są dostępne dla patronów)")]
                return folder
            folder.children = [_programme(p) for p in favourites]
            if not folder.children:
                folder.children = [_folder("progs", "(brak ulubionych — zobacz Audycje A–Z)")]
            return folder
        if kind == "latest":
            page = _page(args, 0)
            return self._listing(
                client,
                lambda p: _ident("latest", p),
                "Najnowsze podcasty",
                page,
                await self._ui_page(client.async_latest, page),
                programme=True,
            )
        if kind == "progs":
            return await self._programmes(client, args[0] if args else None)
        if kind == "prog" and args:
            slug, page = args[0], _page(args, 1)
            prog = await client.async_programme(slug)
            result = await self._ui_page(
                lambda api_page: client.async_programme_episodes(slug, api_page), page
            )
            folder = self._listing(
                client,
                lambda p: _ident("prog", slug, p),
                prog.name if prog else slug,
                page,
                result,
                programme=False,
            )
            folder.thumbnail = prog.image if prog else None
            return folder
        raise BrowseError(f"Unknown folder: {'/'.join(parts)}")

    def _root(self) -> BrowseMediaSource:
        root = _folder("", STATION, LOGO)
        root.children = [
            _folder("live", "Na żywo", LOGO),
            _folder("favs", "Ulubione audycje"),
            _folder(_ident("latest", 1), "Najnowsze podcasty"),
            _folder("progs", "Audycje A–Z"),
        ]
        return root

    def _live(self, client: Radio357Client) -> BrowseMediaSource:
        folder = _folder("live", "Na żywo", LOGO)
        folder.children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=_ident("live", key),
                media_class=MediaClass.CHANNEL,
                media_content_type=mime,
                title=title,
                can_play=True,
                can_expand=False,
                thumbnail=LOGO,
            )
            for key, (title, _, mime, patrons) in LIVE_STREAMS.items()
            if client.is_patron or not patrons
        ]
        return folder

    async def _programmes(self, client: Radio357Client, letter: str | None) -> BrowseMediaSource:
        programmes = await client.async_programmes()
        groups: dict[str, list[Programme]] = {}
        for prog in programmes:
            groups.setdefault(_letter(prog.name), []).append(prog)
        if letter is None:
            folder = _folder("progs", f"Audycje A–Z ({len(programmes)})")
            folder.children = [
                _folder(_ident("progs", key), f"{key} ({len(groups[key])})")
                for key in sorted(groups, key=lambda k: (not k[0].isalpha(), k))
            ]
            return folder
        folder = _folder(_ident("progs", letter), f"Audycje: {letter}")
        folder.children_media_class = MediaClass.PODCAST
        folder.children = [_programme(p) for p in groups.get(letter, [])]
        return folder

    @staticmethod
    async def _ui_page(fetch, page: int) -> tuple[list[Episode], int]:
        """Return (episodes, total) of a PAGE_SIZE page (1-based) over API pages."""
        first = await fetch(0)
        size = max(1, first.page_size)
        start = (page - 1) * PAGE_SIZE
        if start >= first.total:
            return [], first.total
        wanted = range(start // size, (start + PAGE_SIZE - 1) // size + 1)
        pages: list[EpisodePage] = list(
            await asyncio.gather(*(fetch(n) for n in wanted if n * size < first.total))
        )
        episodes = [ep for p in pages for ep in p.episodes]
        offset = start - wanted[0] * size
        return episodes[offset : offset + PAGE_SIZE], first.total

    def _listing(
        self,
        client: Radio357Client,
        ident,
        title: str,
        page: int,
        result: tuple[list[Episode], int],
        *,
        programme: bool,
    ) -> BrowseMediaSource:
        """Return a page of episodes with previous/next page entries."""
        episodes, total = result
        last_page = max(1, -(-total // PAGE_SIZE))
        if last_page > 1:
            title = f"{title} ({page}/{last_page})"
        folder = _folder(ident(page), title)
        folder.children_media_class = MediaClass.PODCAST
        children: list[BrowseMediaSource] = []
        if page > 1:
            children.append(_folder(ident(page - 1), "« Poprzednia strona"))
        children.extend(self._episode(client, ep, programme=programme) for ep in episodes)
        if page < last_page:
            children.append(_folder(ident(page + 1), "Następna strona »"))
        if not episodes:
            children.append(_folder("progs", "(brak podcastów)"))
        folder.children = children
        return folder

    # ------------------------------------------------------------ searching

    async def async_search_media(
        self, item: MediaSourceItem, query: SearchMediaQuery
    ) -> SearchMedia:
        """Search programmes (name, hosts) and the newest episodes."""
        text = query.search_query.strip().casefold()
        if not text:
            return SearchMedia(result=[])
        client = self._client()
        try:
            programmes = await client.async_programmes()
            first = await client.async_latest(0)
            pages = [first]
            more = range(1, min(SEARCH_PAGES, -(-first.total // max(1, first.page_size))))
            pages.extend(await asyncio.gather(*(client.async_latest(n) for n in more)))
        except Radio357Error as err:
            raise BrowseError(str(err)) from err

        matches: list[BrowseMediaSource] = [
            _programme(p)
            for p in programmes
            if text in p.name.casefold() or text in p.hosts.casefold()
        ]
        seen: set[str] = set()
        for page in pages:
            for ep in page.episodes:
                haystack = f"{ep.title} {ep.subtitle} {ep.hosts}".casefold()
                if ep.id not in seen and text in haystack:
                    seen.add(ep.id)
                    matches.append(self._episode(client, ep, programme=True))
        return SearchMedia(result=matches)


def _page(args: list[str], index: int) -> int:
    try:
        return max(1, int(args[index]))
    except (IndexError, ValueError):
        return 1
