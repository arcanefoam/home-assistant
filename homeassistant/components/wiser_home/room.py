"""
This module implements the Room class.
"""
import asyncio
import logging
from collections import namedtuple
import datetime
from enum import Enum

from homeassistant.components.climate.const import (
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.climate.const import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
)
from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_state_change,
)
from .const import (
    CONF_WEEKDAYS,
    DEFAULT_AWAY_TEMP,
    OFF_VALUE,
    TEMP_HYSTERESIS,
)
from .schedule import Schedule, Rule
from .util import RangingSet

_LOGGER = logging.getLogger(__name__)

Thermostat = namedtuple('Thermostat', ['entity_id', 'weight'])
boost_values = {
    "Up": 2,
    "Down": 1,
    "None": 0
}


class HeatingMode(Enum):
    AUTO = 'Auto'
    MANUAL = 'Manual'
    AWAY = 'Away'


class TempDirection(Enum):
    NONE = 0
    HEATING = 1
    COOLING = 2


class Room:
    """A room to be controlled by Wiser Home, identified by a name.
    A rooms has a current room temperature and a current set-point.
    Temperature in a room can be boosted by 2°C, for a period of 30 min,
    1, 2, or 3 hours.
    Rooms can operate in Auto, Manual or Away mode. Away mode is controlled
    by the Wiser Home that contains the room.

    A room can have one or more thermostats. If it has more than one, the
    current room temperature will be the average of the thermostats'
    temperature.

    In automatic mode a room is controlled by its schedule. Schedules are
    defined in the Wiser Home configuration in configuration.yaml.
    If no schedule is defined, the room uses the following schedule:

        Monday – Friday            Saturday – Sunday
          Time       Temp            Time        Temp
        6:30 am     20.0°C          7:00 am     20.0°C
        8:30 am     16.0°C          9:00 am     18.0°C
        4:30 pm     21.0°C          4:00 pm     21.0°C
        10:30 pm    Off*            11:00 pm    Off*

    * Only frost protection is active
    """

    def __init__(self, name, room_data, therms, schedule: Schedule):
        self.hass = None
        self._name = name
        self._current_set_point = 20
        self._current_room_temp = 20
        self._current_temp_direction = TempDirection.HEATING
        self._heating_mode = HeatingMode.AUTO
        self._prev_heating_mode = HeatingMode.AUTO
        self._room_data = room_data
        self._therms = {t.entity_id: t.weight for t in therms}
        self._weight_sum = sum(self._therms.values())
        self._therm_boost = {t.entity_id: 0 for t in therms}
        self._therm_boost_prev = {t.entity_id: 0 for t in therms}
        self._therm_set_point = {t.entity_id: 20 for t in therms}
        self._schedule = schedule
        self._markers = None
        self._heating = False
        self._temp_lock = asyncio.Lock()
        self._away_temp = DEFAULT_AWAY_TEMP
        if not self._schedule.rules:   # Create default rules
            # Week days
            self._schedule.rules.append(
                Rule(
                    value=20,
                    name="wd morning",
                    start_time=datetime.time(6, 0),
                    end_time=datetime.time(8, 30),
                    constraints={CONF_WEEKDAYS: RangingSet(range(1, 6))}
                )
            )
            self._schedule.rules.append(
                Rule(
                    value=16,
                    name="wd day",
                    start_time=datetime.time(8, 30),
                    end_time=datetime.time(16, 30),
                    constraints={CONF_WEEKDAYS: RangingSet(range(1, 6))}
                )
            )
            self._schedule.rules.append(
                Rule(
                    value=21,
                    name="wd evening",
                    start_time=datetime.time(16, 30),
                    end_time=datetime.time(22, 30),
                    constraints={CONF_WEEKDAYS: RangingSet(range(1, 6))}
                )
            )
            # Weekends
            self._schedule.rules.append(
                Rule(
                    value=20,
                    name="we morning",
                    start_time=datetime.time(7, 0),
                    end_time=datetime.time(9, 0),
                    constraints={CONF_WEEKDAYS: RangingSet({6, 7})}
                )
            )
            self._schedule.rules.append(
                Rule(
                    value=18,
                    name="we day",
                    start_time=datetime.time(9, 0),
                    end_time=datetime.time(16, 0),
                    constraints={CONF_WEEKDAYS: RangingSet({6, 7})}
                )
            )
            self._schedule.rules.append(
                Rule(
                    value=21,
                    name="evening",
                    start_time=datetime.time(16, 0),
                    end_time=datetime.time(23, 0),
                    constraints={CONF_WEEKDAYS: RangingSet({6, 7})}
                )
            )
            # Default
            self._schedule.rules.append(
                Rule(
                    value=OFF_VALUE,
                    name="sleep",
                )
            )

    @property
    def name(self):
        return self._name

    @property
    def boost(self):
        return any(self._therm_boost.values())

    def current_temp(self):
        return f'{self._current_room_temp:.1f}'

    def demand_heat(self):
        return self._heating

    def track_thermostats(self, hass):
        """ Request state tracking for room thermostats """
        self.hass = hass
        _LOGGER.debug("Room %s track_thermostats", self._name)
        for t_id in self._therms.keys():
            async_track_state_change(
                hass, t_id, self._async_thermostat_changed
            )

    async def _async_thermostat_changed(self, entity_id, old_state, new_state):
        """Handle temperature changes."""
        if new_state is None:
            return
        self._async_update_state(entity_id, new_state)
        await self._async_control_thermostats()

    @callback
    def _async_update_state(self, entity_id, state):
        """ Update the valve(s) state """
        if entity_id in self._therms:
            # Local temperature
            try:
                local_temp = state.attributes["local_temperature"]
                prev_temp = self._current_room_temp
                if len(self._therms) == 1:
                    self._current_room_temp = local_temp
                else:
                    self._current_room_temp -= (self._current_room_temp * self._therms[entity_id]) / self._weight_sum
                    self._current_room_temp += (local_temp * self._therms[entity_id]) / self._weight_sum
                if prev_temp > self._current_room_temp:
                    self._current_temp_direction = TempDirection.COOLING
                elif prev_temp <= self._current_room_temp:
                    self._current_temp_direction = TempDirection.HEATING
                else:
                    self._current_temp_direction = TempDirection.NONE
                _LOGGER.info("%s temp: %s, -> %s", self._name, self._current_room_temp, self._current_temp_direction.name)
            except KeyError:
                pass
            # Is there a thermostat boost?
            try:
                curr_boost = boost_values[state.attributes["boost"]]
                self._therm_boost[entity_id] = self._therm_boost_prev[entity_id] ^ curr_boost
                self._therm_boost_prev[entity_id] = curr_boost
                _LOGGER.info("%s boost: %s", self._name, self._therm_boost)
            except KeyError:
                pass
            # Current set-points
            try:
                self._therm_set_point[entity_id] = state.attributes["occupied_heating_setpoint"]
            except KeyError:
                pass
        else:
            _LOGGER.warning("Thermostat with id %s is not linked to room %s.", entity_id, self._name)

    async def async_apply_schedule(self, time):
        """Applies the value scheduled for the given time."""
        result = None
        if self._schedule:
            result = await self._schedule.evaluate(self, time)
        if result is None:
            _LOGGER.warning("No suitable value found in schedule. Not changing set-points.")
        else:
            new_scheduled_value, markers = result[:2]
            new_scheduled_value = new_scheduled_value if new_scheduled_value != OFF_VALUE else 5
            if self._heating_mode == HeatingMode.AWAY:
                if new_scheduled_value > self._away_temp:
                    new_scheduled_value = self._away_temp
            self._current_set_point = new_scheduled_value
            self._determine_heating()
            self._markers = markers
        return {"name": self._name,
                "current_temp": self._current_room_temp,
                "current_setpoint": self._current_set_point,
                "heating": self._heating,
                "heating_mode": self._heating_mode.value,
                "therm_boost": self._therm_boost
                }

    @callback
    def _determine_heating(self):
        if self._current_temp_direction == TempDirection.HEATING:
            self._heating = self._current_set_point > self._current_room_temp
        elif self._current_temp_direction == TempDirection.COOLING:
            self._heating = self._current_set_point > (self._current_room_temp + TEMP_HYSTERESIS)
        else:
            self._heating = False
        _LOGGER.info("Room %s demands heat: %s", self._name, self._heating)

    @callback
    def validate_value(self, value):
        return value

    async def _async_control_thermostats(self):
        async with self._temp_lock:
            if self._heating_mode == HeatingMode.AUTO:
                for entity_id, sp in self._therm_set_point.items():
                    if sp != self._current_set_point:
                        _LOGGER.debug("Setting %s set-point to %s", entity_id, self._current_set_point)
                        data = {
                            ATTR_ENTITY_ID: entity_id,
                            ATTR_TEMPERATURE: self._current_set_point
                        }
                        await self.hass.services.async_call(CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE, data)

    async def change_way_mode(self, away, away_temp):
        if away:
            self._prev_heating_mode = self._heating_mode
            self._heating_mode = HeatingMode.AWAY
        else:
            self._heating_mode = self._prev_heating_mode
        self._away_temp = away_temp
