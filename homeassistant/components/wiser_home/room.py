"""
This module implements the Room class.
"""
import asyncio
import logging
from collections import namedtuple
import datetime
from enum import Enum, auto

from homeassistant.components.climate.const import (
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.climate.const import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
)
from homeassistant.core import callback, State
from homeassistant.helpers.event import (
    async_track_state_change,
    async_track_time_interval,
)
from .const import (
    CONF_WEEKDAYS,
    DEFAULT_AWAY_TEMP,
    OFF_VALUE,
    SCHEDULE_INTERVAL,
    TEMP_HYSTERESIS,
)
from .schedule import Schedule, Rule
from .util import RangingSet

_LOGGER = logging.getLogger(__name__)

Thermostat = namedtuple('Thermostat', ['entity_id', 'weight'])
BOOST_UP = 2
BOOST_DOWN = 1
boost_values = {
    "Up": BOOST_UP,
    "Down": BOOST_DOWN,
    "None": 0
}
boost_delta = {
    BOOST_UP: 2,
    BOOST_DOWN: -2,
}


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

    def __init__(self, name=None, valves=None, schedule: Schedule = None):
        if valves is None:
            valves = []
        self.hass = None
        self.schedule = schedule
        self.away_temp = DEFAULT_AWAY_TEMP
        self.boost_all_temp = None
        self.valve_boost = RoomValveBoost()
        self.manual_temp = None
        self.set_point = None
        self._name = name
        self._heating = False
        self._valves = [v.entity_id for v in valves]
        self._room_temp = RoomTemperature(valves={v.entity_id: v.weight for v in valves})
        self._valve_boost_timer_remove = None
        self._room_boost_timer_remove = None
        self._state = Auto()
        self._temp_lock = asyncio.Lock()
        if not self.schedule.rules:   # Create default rules
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
    def room_temp(self):
        return self._room_temp.room_temp()

    @property
    def boost(self):
        boost, _ = self.valve_boost.has_boost()
        return boost

    def format_room_temp(self):
        return f'{self.room_temp:.1f}'

    def demands_heat(self):
        return self._heating

    @callback
    def track_valves(self, hass):
        """
        Request state tracking for room thermostats.
        Called when house is added to Hass
        """
        self.hass = hass
        for t_id in self._valves:
            async_track_state_change(
                hass, t_id, self._async_thermostat_changed
            )

    @callback
    def validate_value(self, value):
        return value

    async def _async_thermostat_changed(self, entity_id: str, old_state: State, new_state: State) -> None:
        """Handle thermostat state changes."""
        if new_state is None:
            return
        await self.valve_state(entity_id, new_state)

    async def valve_state(self, entity_id, new_state):
        await self._async_update_state(entity_id, new_state)
        await self._async_detect_boost(entity_id, new_state)
        await self._async_send_setpoint(entity_id)

    async def _async_update_state(self, entity_id, state):
        """ Update the valve(s) state """
        if entity_id in self._valves:
            self._room_temp.async_local_temp(entity_id, state)

    async def _async_detect_boost(self, entity_id, state):
        """ Update the valve(s) state """
        if entity_id in self._valves:
            await self.valve_boost.async_valve_boost(entity_id, state, self.room_temp)

    async def async_tick(self, time):
        if not isinstance(self._state, Away) and self.boost:
            self._state = self._state.on_event(Event.VALVE_BOOST)
            self._valve_boost_timer_remove = async_track_time_interval(
                self.hass,
                self._async_valve_boost_end,
                datetime.timedelta(hours=1))
        self._do_heating(time)

    @callback
    def _do_heating(self, time):
        self.set_point = await self._state.target_temp(self, time)
        self._heating = self.room_temp.determine_heating(self.set_point)
        _LOGGER.info("Room %s demands heat: %s", self._name, self._heating)

    def attributes(self):
        return {
            "name": self._name,
            "temperature": self.room_temp,
            "setpoint": self.set_point if self.set_point > 5 else OFF_VALUE,
            "heating": self._heating,
            "valve_boost": self.boost
        }

    async def async_away_mode(self, away, setpoint):
        self.away_temp = setpoint
        self._state = self._state.on_event(Event.AWAY_ON if away else Event.AWAY_OFF)
        self._do_heating(datetime.datetime.now())

    async def async_boost_all_mode(self, boost):
        self.boost_all_temp = self.room_temp
        self._state = self._state.on_event(Event.BOOST_ALL if boost else Event.CANCEL_ALL)
        self._do_heating(datetime.datetime.now())

    async def async_manual(self, manual, setpoint):
        self.manual_temp = setpoint
        self._state = self._state.on_event(Event.MANUAL if manual else Event.AUTO)
        self._do_heating(datetime.datetime.now())

    async def async_boost_room(self, setpoint, duration):
        if duration == 0:
            await self.async_auto_mode()
        else:
            self.manual_temp = setpoint
            self._room_boost_timer_remove = async_track_time_interval(
                self.hass,
                self._async_room_boost_end,
                datetime.timedelta(hours=1))
            self._state = self._state.on_event(Event.ROOM_BOOST)
        self._do_heating(datetime.datetime.now())

    async def async_auto_mode(self):
        self._state = self._state.on_event(Event.AUTO)
        self._do_heating(datetime.datetime.now())

    async def _async_valve_boost_end(self, **kwargs):
        self._valve_boost_timer_remove()
        self._state = self._state.on_event(Event.AUTO)
        self._do_heating(datetime.datetime.now())

    async def _async_room_boost_end(self, **kwargs):
        self._room_boost_timer_remove()
        self._state = self._state.on_event(Event.AUTO)
        self._do_heating(datetime.datetime.now())

    async def _async_send_setpoint(self, entity_id):
        async with self._temp_lock:
            data = {
                ATTR_ENTITY_ID: entity_id,
                ATTR_TEMPERATURE: self.set_point
            }
            await self.hass.services.async_call(CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE, data)


class RoomTemperature:

    def __init__(self, valves=None, temp_direction=TempDirection.NONE, room_temp=20):
        if valves is None:
            valves = {}
        self._valves = valves
        self._room_temp = room_temp
        self._temp_direction = temp_direction
        self._weight_sum = sum(self._valves.values())

    @callback
    def async_local_temp(self, entity_id, state):
        # Local temperature
        try:
            local_temp = state.attributes["local_temperature"]
            prev_temp = self._room_temp
            if len(self._valves) == 1:
                self._room_temp = local_temp
            else:
                self._room_temp -= (self._room_temp * self._valves[entity_id]) / self._weight_sum
                self._room_temp += (local_temp * self._valves[entity_id]) / self._weight_sum
            if prev_temp > self._room_temp:
                self._temp_direction = TempDirection.COOLING
            elif prev_temp <= self._room_temp:
                self._temp_direction = TempDirection.HEATING
            else:
                self._temp_direction = TempDirection.NONE
        except KeyError:
            pass

    @property
    def room_temp(self):
        return self._room_temp

    @callback
    def determine_heating(self, target_temp):
        if self._temp_direction == TempDirection.HEATING:
            return target_temp > self._room_temp
        elif self._temp_direction == TempDirection.COOLING:
            return target_temp > (self._room_temp + TEMP_HYSTERESIS)
        else:
            return False


class RoomValveBoost:

    def __init__(self, valve_set_point=None, valve_boost=False, valve_boost_delta=0):
        if valve_set_point is None:
            valve_set_point = {}
        self._valve_set_point = valve_set_point
        self._valve_boost = valve_boost
        self._valve_boost_temp = valve_boost_delta

    def has_boost(self):
        return self._valve_boost, self._valve_boost_temp

    async def async_valve_boost(self, entity_id, state, room_temp):
        """ Is there a valve boost? """
        prev_setpoint = self._valve_set_point[entity_id]
        self._valve_set_point[entity_id] = setpoint = state.attributes["occupied_heating_setpoint"]
        if not self._valve_boost:
            delta = setpoint - prev_setpoint
            # Valve boost is 2°
            if delta > 1.5 and state.attributes["boost"] == "Up":
                _LOGGER.info("%s BOOST UP", entity_id)
                self._valve_boost_temp = room_temp + 2
                self._valve_boost = True
            elif delta < 1.5 and state.attributes["boost"] == "Down":
                _LOGGER.info("%s BOOST DOWN", entity_id)
                self._valve_boost_temp = room_temp - 2
                self._valve_boost = True
            else:
                self._valve_boost_temp = room_temp
                self._valve_boost = False


class RoomState(object):
    """
    We define a state object which provides some utility functions for the
    individual states within the state machine.
    """

    def __init__(self):
        print('Processing current state:', str(self))

    def on_event(self, event):
        """
        Handle events that are delegated to this State.
        """
        pass

    async def target_temp(self, room, time):
        pass

    def __repr__(self):
        """
        Leverages the __str__ method to describe the State.
        """
        return self.__str__()

    def __str__(self):
        """
        Returns the name of the State.
        """
        return self.__class__.__name__


class Event(Enum):
    AWAY_ON = auto()
    AWAY_OFF = auto()
    BOOST_ALL = auto()
    CANCEL_ALL = auto()
    VALVE_BOOST = auto()
    MANUAL = auto()
    ROOM_BOOST = auto()
    AUTO = auto()


class Auto(RoomState):
    """
    The Room is in Auto mode, following the shcedule
    """

    def on_event(self, event):
        if event == Event.AWAY_ON:
            return Away()
        elif event == Event.BOOST_ALL:
            return HouseBoost()
        elif event == Event.VALVE_BOOST:
            return RoomValveBoost()
        elif event == Event.ROOM_BOOST:
            return RoomBoost()
        return self

    async def target_temp(self, room, time):
        result = None
        if room.schedule is not None:
            result = await room.schedule.evaluate(self, time)
        if result is None:
            _LOGGER.warning("No suitable value found in schedule. Not changing set-points.")
            result = room.set_point
        else:
            new_scheduled_value, _ = result[:2]
            result = new_scheduled_value if new_scheduled_value != OFF_VALUE else 5
        return result


class Away(RoomState):
    """
    The state which indicates that house is in away mode
    """

    def on_event(self, event):
        if event == Event.AWAY_OFF:
            return Auto()
        return self

    async def target_temp(self, room, time):
        return room.away_temp


class HouseBoost(RoomState):
    """
    The state which indicates that the house is in boost all
    """

    def on_event(self, event):
        if event == Event.CANCEL_ALL:
            return Auto()
        elif event == Event.VALVE_BOOST:
            return RoomValveBoost()
        elif event == Event.MANUAL:
            return Manual()
        elif event == Event.ROOM_BOOST:
            return RoomBoost()
        return self

    async def target_temp(self, room, time):
        return room.boost_all_temp


class ValveBoost(RoomState):
    """
    The state which indicates that the room is in boost via valve
    """

    def on_event(self, event):
        if event == Event.AUTO:
            return Auto()
        elif event == Event.MANUAL:
            return Manual()
        return self

    async def target_temp(self, room, time):
        _, target_temp = room.valve_boost.has_boost()
        return target_temp


class Manual(RoomState):
    """
    The state which indicates that the room is in manual mode
    """

    def on_event(self, event):
        if event == Event.AWAY_ON:
            return Away()
        elif event == Event.MANUAL:
            return Manual()
        elif event == Event.VALVE_BOOST:
            return RoomValveBoost()
        elif event == Event.AUTO:
            return Auto()
        return self

    async def target_temp(self, room, time):
        return room.manual_temp


class RoomBoost(RoomState):
    """
    The state which indicates that the room is in boost via room
    """

    def on_event(self, event):
        if event == Event.AUTO:
            return Auto()
        elif event == Event.VALVE_BOOST:
            return RoomValveBoost()

    async def target_temp(self, room, time):
        return room.manual_temp
