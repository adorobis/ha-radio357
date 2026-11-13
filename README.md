# Radio 357 for Home Assistant

[![Marketplace][marketplace-badge]][marketplace-url]
[![Validate][validate-badge]][validate-url]

Unofficial Home Assistant integration for [Radio 357](https://radio357.pl): browse and
play the live stream and the podcast archive on any media player (MusicCast, Sonos,
Chromecast, the browser, …) from the Home Assistant media browser.

> **Not affiliated with Radio 357.** It uses the same JSON API as radio357.pl and the
> station's apps, logged in with your own account. Most podcasts need a logged-in
> account and many need an active patron subscription. If the API changes, the
> integration may stop working until it is updated.

[Po polsku ↓](#po-polsku)

## Features

- **Media source** "Radio 357" in the media browser:
  - **Na żywo**: live radio as 128 kbps MP3 and 128 kbps AAC. Patron accounts also get
    the patron variant of the stream.
  - **Ulubione audycje**: the programmes you marked as favourite in your account.
  - **Najnowsze podcasty**: the newest episodes of all programmes, 50 per page.
  - **Audycje A–Z**: all programmes (about 470), grouped by first letter. Each one opens
    its episodes, newest first, 50 per page.
- **Search** across programme names, hosts and the titles of the newest ~1000 episodes.
- **Access levels** are read from your account. Episodes your account can't play are
  shown with 🔒 and the level they need, and can't be started.
- **Audio proxy**: players get a plain local `http://` URL from Home Assistant, so they
  don't have to follow the HTTPS redirect chain. Episodes support byte ranges
  (seeking). If the audio host drops the connection, the proxy reconnects at the exact
  byte and fetches a new signed URL if the old one has expired. Live streams reconnect
  as well.
- **Metadata for players** (title, hosts, cover, duration, size). With
  [MusicCast Extras](https://github.com/adorobis/ha-musiccast-extras) a Yamaha speaker
  shows them and can seek and resume episodes.
- Polish and English UI translations.

## Requirements

- Home Assistant 2026.11 or newer.
- A Radio 357 account ([konto.radio357.pl](https://konto.radio357.pl)). A patron
  subscription unlocks the patron-only episodes and the patron stream.
- Players must be able to reach Home Assistant's local URL
  (Settings → System → Network).

## Installation

### Marketplace (recommended)

Install it from the Home Assistant **Marketplace** as a custom repository:

1. Settings → **Marketplace**. Adding custom repositories needs the Marketplace to be connected
   to GitHub; it asks for that the first time.
2. ⋮ → **Custom repositories** → add `https://github.com/adorobis/ha-radio357`, type
   **Integration**.
3. Open **Radio 357**, **Download** it and restart Home Assistant.

Or open the repository in your Marketplace directly:

[![Open in your Home Assistant Marketplace][my-badge]][my-url]

Updates then show up in Home Assistant like any other update.

### Manual

Copy `custom_components/radio357` into your `config/custom_components/` folder and
restart Home Assistant.

## Configuration

Settings → Devices & services → **Add integration** → **Radio 357**, then log in with the
e-mail and password of your Radio 357 account. If the password is changed later, Home
Assistant asks you to log in again.

## Usage

Open **Media** in the sidebar (or *Browse media* on a player card), pick the player and
go to **Radio 357**.

To play from automations, scripts or dashboard buttons use `media_player.play_media`:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room
data:
  media_content_id: media-source://radio357/live/mp3
  media_content_type: music
```

| `media_content_id` | Plays |
| --- | --- |
| `media-source://radio357/live/mp3` | Live, 128 kbps MP3 |
| `media-source://radio357/live/aac` | Live, 128 kbps AAC |
| `media-source://radio357/live/patron` | Live, patron variant (AAC, patron accounts) |
| `media-source://radio357/ep/<id>` | One episode |

Folders can be opened the same way, e.g. `media-source://radio357/prog/<slug>/1` for a
programme (`<slug>` is the last part of its address on radio357.pl).

### For player integrations

Players that push URLs themselves (for example to a DLNA renderer) can get the title,
hosts, cover and duration of an item from the optional provider registered in
`hass.data["media_info_providers"]["radio357"]`
(`async (hass, media_content_id) -> dict | None`, see
[`info.py`](custom_components/radio357/info.py)).

## Privacy and security

- The e-mail and password are stored in the Home Assistant config entry, like any
  other integration's credentials, and are only sent to Radio 357's login service
  (`auth.r357.eu`). Access tokens are kept in memory only.
- Audio is served at `/api/radio357/audio/…`, which requires Home Assistant
  authentication (players get a signed URL).
- Cover images are relayed at `/api/radio357/image/…` **without** authentication,
  because speakers fetch album art without credentials. It only relays public images
  from Radio 357's media hosts.

## Troubleshooting

Enable debug logging and include the log in an issue:

```yaml
logger:
  logs:
    custom_components.radio357: debug
```

---

## Po polsku

Nieoficjalna integracja Home Assistant dla Radia 357. Pozwala przeglądać i odtwarzać
radio na żywo oraz archiwum podcastów na dowolnym odtwarzaczu (MusicCast, Sonos,
Chromecast, przeglądarka…) z poziomu przeglądarki mediów w Home Assistant.

- **Na żywo** (MP3 i AAC 128 kb/s, a dla patronów także wersja patronacka),
  **Ulubione audycje**, **Najnowsze podcasty** i **Audycje A–Z**, z podziałem na strony.
- **Wyszukiwanie** po nazwach audycji, prowadzących i tytułach najnowszych odcinków.
- Odcinki niedostępne dla Twojego konta są oznaczone 🔒 z informacją, dla kogo są.
- Przewijanie odcinków, a z [MusicCast Extras](https://github.com/adorobis/ha-musiccast-extras)
  także tytuł, okładka, czas trwania i wznawianie na głośnikach Yamaha.
- Wymagane konto Radia 357. Odcinki dla patronów wymagają aktywnego patronatu.

**Instalacja:** Ustawienia → **Marketplace** (przy pierwszym dodawaniu repozytorium trzeba
połączyć Marketplace z GitHubem) → ⋮ → *Custom repositories* →
`https://github.com/adorobis/ha-radio357` (typ *Integration*) → pobierz **Radio 357** →
restart HA → Ustawienia → Urządzenia i usługi → *Dodaj integrację* → **Radio 357** →
zaloguj się e-mailem i hasłem z konto.radio357.pl. Aktualizacje pojawią się potem w HA
jak każde inne.

Integracja nie jest powiązana z Radiem 357. Korzysta z tego samego API co strona i
aplikacje stacji, więc jego zmiany mogą wymagać aktualizacji integracji.

## License

[MIT](LICENSE)

[marketplace-badge]: https://img.shields.io/badge/Marketplace-custom%20repository-41BDF5.svg
[marketplace-url]: https://www.home-assistant.io/integrations/marketplace
[validate-badge]: https://github.com/adorobis/ha-radio357/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/adorobis/ha-radio357/actions/workflows/validate.yml
[my-badge]: https://my.home-assistant.io/badges/marketplace_repository.svg
[my-url]: https://my.home-assistant.io/redirect/marketplace_repository/?owner=adorobis&repository=ha-radio357&category=integration
