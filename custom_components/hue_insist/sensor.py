"""Diagnostics: what is Hue Insist actually doing, and is anything wrong.

A watchdog you cannot observe is indistinguishable from one that is broken. The
first group of sensors answers "is it running": how many requests were caught,
split by scene, group and individual lamp. The second answers "is anything
wrong": how many lamp-checks passed, how many needed a nudge, and how many could
not be fixed at all.

The ratio between checked devices and corrections is the interesting number. It
turns "I think that lamp misses commands sometimes" into evidence, and it names
the lamp.

Counters survive a restart. Without that they reset to zero on every reload, and
a counter that keeps starting over tells you nothing about a problem that happens
a few times a day.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SIGNAL_STATS_UPDATED
from .stats import Stats


@dataclass(frozen=True, kw_only=True)
class InsistSensorDescription(SensorEntityDescription):
    """A sensor plus how to read its value out of the running statistics."""

    value: Callable[[Stats], int | datetime | None]
    counter: str | None = None      # attribute to restore into after a restart


COUNTERS: tuple[InsistSensorDescription, ...] = (
    InsistSensorDescription(
        key="captured_actions",
        name="Captured actions",
        icon="mdi:import",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: s.actions_total,
        counter="actions_total",
    ),
    InsistSensorDescription(
        key="captured_scene_actions",
        name="Captured scene actions",
        icon="mdi:palette",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: s.actions_scene,
        counter="actions_scene",
    ),
    InsistSensorDescription(
        key="captured_group_actions",
        name="Captured group actions",
        icon="mdi:lightbulb-group",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: s.actions_group,
        counter="actions_group",
    ),
    InsistSensorDescription(
        key="captured_device_actions",
        name="Captured device actions",
        icon="mdi:lightbulb",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: s.actions_device,
        counter="actions_device",
    ),
    InsistSensorDescription(
        key="checked_devices",
        name="Checked devices",
        icon="mdi:clipboard-check-outline",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: s.devices_checked,
        counter="devices_checked",
    ),
    InsistSensorDescription(
        key="no_correction_needed",
        name="No correction needed",
        icon="mdi:check-circle-outline",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: s.devices_ok,
        counter="devices_ok",
    ),
    InsistSensorDescription(
        key="corrections",
        name="Corrections",
        icon="mdi:auto-fix",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: s.corrections,
        counter="corrections",
    ),
    InsistSensorDescription(
        key="failures",
        name="Failures",
        icon="mdi:lightbulb-alert",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: s.failures,
        counter="failures",
    ),
)

TIMESTAMPS: tuple[InsistSensorDescription, ...] = (
    InsistSensorDescription(
        key="last_action",
        name="Last action",
        icon="mdi:clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda s: s.last_action,
    ),
    InsistSensorDescription(
        key="last_correction",
        name="Last correction",
        icon="mdi:clock-alert-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda s: s.last_correction,
    ),
    InsistSensorDescription(
        key="last_failure",
        name="Last failure",
        icon="mdi:clock-remove-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda s: s.last_failure,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback
) -> None:
    watcher = hass.data[DOMAIN][entry.entry_id]
    add([InsistSensor(entry, watcher, d) for d in COUNTERS + TIMESTAMPS])


class InsistSensor(SensorEntity, RestoreEntity):
    """One diagnostic value, refreshed whenever the watcher reports something."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description: InsistSensorDescription

    def __init__(self, entry: ConfigEntry, watcher, description) -> None:
        self._watcher = watcher
        self._entry = entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Hue Insist",
            "manufacturer": "Hue Insist",
            "entry_type": "service",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Pick the counter back up where it left off. Reloading the integration
        # or restarting Home Assistant should not erase the evidence.
        if self.entity_description.counter:
            previous = await self.async_get_last_state()
            if previous and previous.state not in (None, "unknown", "unavailable"):
                try:
                    restored = int(float(previous.state))
                except ValueError:
                    restored = 0
                current = getattr(self._watcher.stats, self.entity_description.counter)
                if restored > current:
                    setattr(
                        self._watcher.stats, self.entity_description.counter, restored
                    )

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_STATS_UPDATED, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Refresh from the event loop.

        Marked as a callback on purpose. An unmarked listener is run by Home
        Assistant in an executor thread, and async_write_ha_state may only be
        called from the event loop -- that combination raises a RuntimeError on
        every single update.
        """
        self.async_write_ha_state()

    @property
    def native_value(self):
        return self.entity_description.value(self._watcher.stats)

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.entity_description.key == "last_failure":
            return {"entities": self._watcher.stats.last_failed_entities}
        return None
