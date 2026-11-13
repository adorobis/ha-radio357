"""Constants for the Radio 357 integration."""

DOMAIN = "radio357"
AUDIO_VIEW_URL = "/api/radio357/audio"
IMAGE_VIEW_URL = "/api/radio357/image"

STREAM_URL = "https://stream.radio357.pl/"
NOW_PLAYING_URL = "https://stream.radio357.pl/now/playing.json"

# Live variants served by stream.radio357.pl (key -> (title, ?s= value, mime,
# patrons only)). Each request is redirected to a short-lived revma URL, so
# the proxy always starts from stream.radio357.pl.
LIVE_STREAMS: dict[str, tuple[str, str, str, bool]] = {
    "mp3": ("Radio 357 (na żywo, MP3 128 kbps)", "app", "audio/mpeg", False),
    "aac": ("Radio 357 (na żywo, AAC 128 kbps)", "web", "audio/aac", False),
    "patron": ("Radio 357 (na żywo, AAC, wersja dla patronów)", "www", "audio/aac", True),
}
LOGO = "https://media.radio357.pl/2023/08/69019663-8230-41d2-ad39-e7efecc675ec.jpg"
STATION = "Radio 357"
