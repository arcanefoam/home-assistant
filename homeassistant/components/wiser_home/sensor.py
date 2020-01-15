"""Adds support for virtual Wiser Home"""
import asyncio
import datetime
import logging

import voluptuous as vol

from homeassistant.components.climate import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_NAME,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import (
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    DEFAULT_NAME,
    CONF_BOILER,
    CONF_ROOMS,
    CONF_THERM,
    SCHEDULE_INTERVAL,
)
from .config import parse_rooms, CONFIG_SCHEMA

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    CONFIG_SCHEMA
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the generic thermostat platform."""
    name = config.get(CONF_NAME)
    boiler = config.get(CONF_BOILER)
    rooms = parse_rooms(config)
    async_add_entities(
        [
            WiserHome(name, boiler, rooms)
        ])
    return True


class WiserHome(RestoreEntity):
    """ Implementation of a Virtual Wiser Heat App """
    
    def __init__(self, name, boiler, rooms):
        self._name = name
        self.boiler = boiler
        self.rooms = rooms
        self._attributes = {}
        self._room_for_entity = {}
        for room in rooms:
            self._attributes[room.name] = STATE_UNKNOWN

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return "ON"

    @property
    def icon(self):
        """Return the icon of the sensor."""
        return "mdi:plex"

    @property
    def unit_of_measurement(self):
        """Return the unit this state is expressed in."""
        return ""

    @property
    def device_state_attributes(self):
        """Return attributes for the sensor."""
        return self._attributes
    
    async def async_update(self):
        """Retrieve latest state."""
        pass

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        # Add listener for each thermostat
        for room in self.rooms:
            room.track_thermostats(self.hass)
        # Add a time internal to evaluate schedules
        async_track_time_interval(self.hass, self._async_control_heating, datetime.timedelta(minutes=SCHEDULE_INTERVAL))

    async def _async_control_heating(self, time):
        for room in self.rooms:
            state = await room.async_apply_schedule(time)
            # state = room.get_state()
            # Apply v to room entities
            _LOGGER.debug("Room %s state", room.name, state)
            self._attributes.update(state)



