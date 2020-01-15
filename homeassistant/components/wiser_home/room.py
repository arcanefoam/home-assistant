"""
This module implements the Room class.
"""
import logging
from collections import namedtuple
import datetime
from enum import Enum, auto

from cached_property import cached_property
from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_state_change,
)

from .const import (
    CONF_WEEKDAYS,
    OFF_VALUE,
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
    AUTO = 'auto'
    MANUAL = 'manual'
    AWAY = 'away'


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
        self._name = name
        self._current_set_point = None
        self._current_room_temp = 20
        self._heating_mode = HeatingMode.AUTO

        _LOGGER.debug("room init %s", room_data)
        self._room_data = room_data
        self._therms = {t.entity_id: t.weight for t in therms}
        self._weight_sum = sum(self._therms.values())
        self._therm_boost = {t.entity_id: 0 for t in therms}
        self._therm_boost_prev = {t.entity_id: 0 for t in therms}
        self._therm_set_point = {t.entity_id: 20 for t in therms}

        self._schedule = schedule

        self._heating = False

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

    def current_room_temp(self):
        return f'{self._current_room_temp:.1f}'

    def track_thermostats(self, hass):
        """ Request state tracking for room thermostats """
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
        # await self.async_update_ha_state()

    @callback
    def _async_update_state(self, entity_id, state):
        """ Update the valve(s) state """
        if entity_id in self._therms:
            # Local temperature
            try:
                local_temp = state.attributes["local_temperature"]
                if len(self._therms) == 1:
                    self._current_room_temp = local_temp
                else:
                    self._current_room_temp -= (self._current_room_temp * self._therms[entity_id]) / self._weight_sum
                    self._current_room_temp += (local_temp * self._therms[entity_id]) / self._weight_sum
                _LOGGER.debug("Current temp: %s", self._current_room_temp)
            except KeyError:
                pass
            # Is there a thermostat boost?
            try:
                curr_boost = boost_values[state.attributes["boost"]]
                self._therm_boost[entity_id] = self._therm_boost_prev[entity_id] ^ curr_boost
                self._therm_boost_prev[entity_id] = curr_boost
                _LOGGER.debug("Boost: %s", self._therm_boost)
            except KeyError:
                pass
            # Current set-points
            try:
                self._therm_set_point[entity_id] = state.attributes["occupied_heating_setpoint"]
            except KeyError:
                pass
        else:
            _LOGGER.warning("Thermostat with id %s is not linked to room %s.", entity_id, self._name)

    async def async_apply_schedule(self, time) -> {}:
        """Applies the value scheduled for the given time."""
        result = None
        if self._schedule:
            result = await self._schedule.evaluate(self, time)
        if result is None:
            _LOGGER.warning("No suitable value found in schedule. Not changing set-points.")
        else:
            new_scheduled_value, markers = result[:2]
            # TODO Send commands to valves
            #if not new_scheduled_value != self._current_set_point:    # Skip when scheduled value hasn't changed
            #    _LOGGER.debug("Result didn't change, not setting it again.")
            #    return self._current_set_point
            self._current_set_point = new_scheduled_value
            self._heating = self._current_set_point > self._current_room_temp
            self._markers = markers

    #@callback
    #def get_state(self):
        return {f"{self._name}.current_temp": self._current_room_temp,
                f"{self._name}.current_setpoint": self._current_set_point,
                f"{self._name}.heating": self._heating,
                f"{self._name}.heating_mode": self._heating_mode.value,
                f"{self._name}.therm_boost": self._therm_boost
                }

    @callback
    def validate_value(self, value):
        """A wrapper around self.app.actor_type.validate_value() that
        sanely logs validation errors and returns None in that case."""
        #
        # assert self.app.actor_type is not None
        # try:
        #     value = self.app.actor_type.validate_value(value)
        # except ValueError as err:
        #     _LOGGER.error(
        #         "Invalid value {} for actor type {}: {}".format(
        #             repr(value), repr(self.app.actor_type.name), err
        #         ),
        #         level="ERROR",
        #     )
        #     return None
        return value