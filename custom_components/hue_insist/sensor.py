"""Diagnostics: how often did Hue Insist have to step in, and where.

This is what makes the integration more than a repair job. Right now there is no
visibility at all into how often a lamp misses a command -- the suspicion exists,
the numbers do not. These sensors make it measurable, so you can tell whether a
lamp has structurally poor range or whether it is incidental.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_CORRECTED, EVENT_FAILED


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    watcher = hass.data[DOMAIN][entry.entry_id]
    add([CorrectionsSensor(entry, watcher), FailuresSensor(entry, watcher),
         LastFailureSensor(entry, watcher)])


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, watcher) -> None:
        self._watcher = watcher
        self._entry = entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Hue Insist",
            "manufacturer": "Hue Insist",
            "entry_type": "service",
        }

    async def async_added_to_hass(self) -> None:
        for event_type in (EVENT_CORRECTED, EVENT_FAILED):
            self.async_on_remove(
                self.hass.bus.async_listen(event_type, lambda _e: self.async_write_ha_state())
            )


class CorrectionsSensor(_Base):
    _attr_name = "Corrections"
    _attr_icon = "mdi:auto-fix"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_corrections"

    @property
    def native_value(self) -> int:
        return self._watcher.corrections


class FailuresSensor(_Base):
    _attr_name = "Failures"
    _attr_icon = "mdi:lightbulb-alert"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_failures"

    @property
    def native_value(self) -> int:
        return self._watcher.failures


class LastFailureSensor(_Base):
    _attr_name = "Last failure"
    _attr_icon = "mdi:alert-circle-outline"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_last_failure"

    @property
    def native_value(self) -> str:
        return (self._watcher.last_failure or "none")[:255]
