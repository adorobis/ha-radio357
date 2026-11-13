"""Radio 357: live radio and podcasts as a media source."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from . import info
from .api import AuthError, Radio357Client, Radio357Error
from .audio import Radio357AudioView, Radio357ImageView
from .const import DOMAIN

type Radio357ConfigEntry = ConfigEntry[Radio357Client]


def create_client(hass: HomeAssistant, email: str, password: str) -> Radio357Client:
    """Return a client with its own session (no shared cookies)."""
    session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
    return Radio357Client(session, email, password)


async def async_setup_entry(hass: HomeAssistant, entry: Radio357ConfigEntry) -> bool:
    """Log in and expose the media source."""
    client = create_client(hass, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    try:
        await client.async_login()
    except AuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Radio357Error as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = client

    if not hass.data.get(f"{DOMAIN}_view"):
        hass.http.register_view(Radio357AudioView(hass))
        hass.http.register_view(Radio357ImageView(hass))
        hass.data[f"{DOMAIN}_view"] = True
    info.register(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Radio357ConfigEntry) -> bool:
    """Nothing to tear down; the session is closed with Home Assistant."""
    return True
