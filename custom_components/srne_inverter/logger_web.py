"""Read the logger serial from the stick's own web page.

The LSW-5 exposes http://<logger>/status.html behind basic auth admin/admin,
with the serial embedded as:  var cover_mid = "3548208972";

UDP discovery on 48899 is deliberately not used: it does not cross subnets,
which is exactly why this integration exists.
"""

from __future__ import annotations

import re

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_COVER_MID = re.compile(r"cover_mid\s*=\s*[\"']([0-9]+)[\"']")

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
DEFAULT_TIMEOUT = 10


class LoggerWebError(Exception):
    """status.html could not be fetched or did not contain the serial."""


async def async_fetch_logger_serial(
    hass: HomeAssistant,
    host: str,
    *,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Return the logger serial found in status.html."""
    session = async_get_clientsession(hass)
    url = f"http://{host}/status.html"
    try:
        async with session.get(
            url,
            auth=aiohttp.BasicAuth(username, password),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status != 200:
                raise LoggerWebError(f"{url} answered HTTP {response.status}")
            body = await response.text()
    except LoggerWebError:
        raise
    except Exception as err:  # noqa: BLE001 - aiohttp/OS errors alike
        raise LoggerWebError(f"cannot read {url}: {err}") from err

    match = _COVER_MID.search(body)
    if match is None:
        raise LoggerWebError(f"{url} did not contain cover_mid")
    return match.group(1)
