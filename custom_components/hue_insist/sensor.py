"""Diagnostiek: hoe vaak moest Hue Insist ingrijpen, en waar.

Dit is de reden dat de integratie meer is dan een reparatie. Nu is er geen enkel
zicht op hoe vaak een lamp een commando mist -- het vermoeden bestaat, de cijfers
niet. Deze sensoren maken het meetbaar, zodat je kunt zien of een lamp structureel
slecht bereik heeft of dat het incidenteel is.
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
    add([CorrectieSensor(entry, watcher), MislukkingSensor(entry, watcher),
         LaatsteFoutSensor(entry, watcher)])


class _Basis(SensorEntity):
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
        for gebeurtenis in (EVENT_CORRECTED, EVENT_FAILED):
            self.async_on_remove(
                self.hass.bus.async_listen(gebeurtenis, lambda _e: self.async_write_ha_state())
            )


class CorrectieSensor(_Basis):
    _attr_name = "Correcties"
    _attr_icon = "mdi:auto-fix"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_correcties"

    @property
    def native_value(self) -> int:
        return self._watcher.correcties


class MislukkingSensor(_Basis):
    _attr_name = "Mislukt"
    _attr_icon = "mdi:lightbulb-alert"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_mislukt"

    @property
    def native_value(self) -> int:
        return self._watcher.mislukkingen


class LaatsteFoutSensor(_Basis):
    _attr_name = "Laatste mislukking"
    _attr_icon = "mdi:alert-circle-outline"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_laatste_fout"

    @property
    def native_value(self) -> str:
        return (self._watcher.laatste_fout or "geen")[:255]
