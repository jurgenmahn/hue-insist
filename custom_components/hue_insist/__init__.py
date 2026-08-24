"""Hue Insist -- makes sure a light command is actually carried out.

A Hue group or scene goes out as a Zigbee groupcast. Groupcast is not
acknowledged per lamp and therefore never retried: a bulb with marginal range
misses the message for good, and Home Assistant never notices because the group
already counts as "on" once one of its members lights up.

This integration catches every light request that passes through Home Assistant,
translates it into a concrete end state per lamp -- for scenes straight from the
definition on the bridge, brightness and colour included -- checks after a short
pause what actually happened, and corrects deviating lamps one by one. Those
single-target commands go out as unicast and do get acknowledged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CALL_SERVICE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_BRIGHTNESS_TOLERANCE,
    CONF_CHECK_BRIGHTNESS,
    CONF_CHECK_COLOR,
    CONF_DELAY,
    CONF_EXCLUDED,
    CONF_MIREK_TOLERANCE,
    CONF_RETRIES,
    CONF_SKIP_UNAVAILABLE,
    CONF_UNAVAILABLE_EXCEPTIONS,
    CONF_WATCH_GROUPS,
    CONF_WATCH_LIGHTS,
    CONF_WATCH_SCENES,
    DEFAULT_BRIGHTNESS_TOLERANCE,
    DEFAULT_DELAY,
    DEFAULT_MIREK_TOLERANCE,
    DEFAULT_RETRIES,
    DEFAULT_SKIP_UNAVAILABLE,
    DOMAIN,
)
from .hue_api import HueDefinitions
from .watcher import Watcher

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]

# The bridge is re-read periodically so a new or changed scene is picked up
# without needing a restart.
REFRESH_INTERVAL = timedelta(minutes=30)


@dataclass
class Options:
    retries: int
    delay: float
    watch_lights: bool
    watch_groups: bool
    watch_scenes: bool
    check_brightness: bool
    check_color: bool
    excluded: set[str]
    brightness_tolerance: int
    mirek_tolerance: int
    skip_unavailable: bool
    unavailable_exceptions: set[str]

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> "Options":
        o = entry.options
        return cls(
            retries=int(o.get(CONF_RETRIES, DEFAULT_RETRIES)),
            delay=float(o.get(CONF_DELAY, DEFAULT_DELAY)),
            watch_lights=bool(o.get(CONF_WATCH_LIGHTS, True)),
            watch_groups=bool(o.get(CONF_WATCH_GROUPS, True)),
            watch_scenes=bool(o.get(CONF_WATCH_SCENES, True)),
            check_brightness=bool(o.get(CONF_CHECK_BRIGHTNESS, True)),
            check_color=bool(o.get(CONF_CHECK_COLOR, True)),
            excluded=set(o.get(CONF_EXCLUDED, [])),
            brightness_tolerance=int(o.get(CONF_BRIGHTNESS_TOLERANCE, DEFAULT_BRIGHTNESS_TOLERANCE)),
            mirek_tolerance=int(o.get(CONF_MIREK_TOLERANCE, DEFAULT_MIREK_TOLERANCE)),
            skip_unavailable=bool(o.get(CONF_SKIP_UNAVAILABLE, DEFAULT_SKIP_UNAVAILABLE)),
            unavailable_exceptions=set(o.get(CONF_UNAVAILABLE_EXCEPTIONS, [])),
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    definitions = HueDefinitions(hass)
    await definitions.async_refresh()

    watcher = Watcher(hass, definitions, Options.from_entry(entry))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = watcher

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_CALL_SERVICE, watcher.handle_event)
    )

    async def _ververs(_now) -> None:
        await definitions.async_refresh()

    entry.async_on_unload(
        async_track_time_interval(hass, _ververs, REFRESH_INTERVAL)
    )
    entry.async_on_unload(entry.add_update_listener(_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info(
        "Hue Insist active: %d attempts, %.1fs delay, bridge %s",
        watcher.options.retries,
        watcher.options.delay,
        "reachable" if definitions.available else "unreachable",
    )
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply new options immediately, without a restart."""
    watcher: Watcher = hass.data[DOMAIN][entry.entry_id]
    watcher.options = Options.from_entry(entry)
    await watcher.definitions.async_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok
