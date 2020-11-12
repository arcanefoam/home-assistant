"""Test the Nightscout config flow."""
from aiohttp import ClientError

from homeassistant.components.nightscout.const import DOMAIN
from homeassistant.config_entries import (
    ENTRY_STATE_LOADED,
    ENTRY_STATE_NOT_LOADED,
    ENTRY_STATE_SETUP_RETRY,
)
from homeassistant.const import CONF_URL

from tests.async_mock import patch
from tests.common import MockConfigEntry
from tests.components.nightscout import init_integration


async def test_unload_entry(hass):
    """Test successful unload of entry."""
    entry = await init_integration(hass)

    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.state == ENTRY_STATE_LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state == ENTRY_STATE_NOT_LOADED
    assert not hass.data.get(DOMAIN)


async def test_async_setup_raises_entry_not_ready(hass):
    """Test that it throws ConfigEntryNotReady when exception occurs during setup."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_URL: "https://some.url:1234"},
    )
    config_entry.add_to_hass(hass)

    with patch(
        "homeassistant.components.nightscout.NightscoutAPI.get_server_status",
        side_effect=ClientError(),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state == ENTRY_STATE_SETUP_RETRY
