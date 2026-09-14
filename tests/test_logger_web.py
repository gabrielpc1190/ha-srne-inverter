"""Serial scraping from the logger's status.html."""

import pytest
from aiohttp import BasicAuth

from custom_components.srne_inverter.logger_web import (
    LoggerWebError,
    async_fetch_logger_serial,
)

PAGE = """
<script>
var cover_sta = "Connected";
var cover_mid = "3548208972";
var cover_ver = "LSW5_01_2421_SS_00_00.00.00.18";
</script>
"""


async def test_serial_is_parsed_from_cover_mid(hass, aioclient_mock):
    aioclient_mock.get("http://192.168.188.240/status.html", text=PAGE)
    assert await async_fetch_logger_serial(hass, "192.168.188.240") == "3548208972"


async def test_missing_cover_mid_raises(hass, aioclient_mock):
    aioclient_mock.get("http://192.168.188.240/status.html", text="<html></html>")
    with pytest.raises(LoggerWebError):
        await async_fetch_logger_serial(hass, "192.168.188.240")


async def test_http_error_raises(hass, aioclient_mock):
    aioclient_mock.get("http://192.168.188.240/status.html", status=401)
    with pytest.raises(LoggerWebError):
        await async_fetch_logger_serial(hass, "192.168.188.240")
