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
    CONF_COMMAND_RATE,
    CONF_CHECK_BRIGHTNESS,
    CONF_CHECK_COLOR,
    CONF_DEBUG_LOG,
    CONF_DELAY,
    CONF_EXCLUDED,
    CONF_MIREK_TOLERANCE,
    CONF_RETRIES,
    CONF_SETTLE_TIMEOUT,
    CONF_SKIP_UNAVAILABLE,
    CONF_UNAVAILABLE_EXCEPTIONS,
    CONF_WATCH_GROUPS,
    CONF_WATCH_LIGHTS,
    CONF_WATCH_SCENES,
    DEFAULT_BRIGHTNESS_TOLERANCE,
    DEFAULT_COMMAND_RATE,
    DEFAULT_DEBUG_LOG,
    DEFAULT_DELAY,
    DEFAULT_MIREK_TOLERANCE,
    DEFAULT_RETRIES,
    DEFAULT_SETTLE_TIMEOUT,
    DEFAULT_SKIP_UNAVAILABLE,
    DOMAIN,
)
from .flash import async_register as async_register_flash
from .hue_api import HueDefinitions
from .snapshots import async_register as async_register_snapshots
from .watcher import Watcher

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]

# Whatever level was configured before we touched it -- through the logger
# integration, say. Turning the option off restores that rather than imposing a
# level of our own on a deliberate setting.
_PACKAGE_LOGGER = logging.getLogger(__package__)
_CONFIGURED_LEVEL = _PACKAGE_LOGGER.level


def _apply_log_level(debug: bool) -> None:
    """Switch verbose logging on or off without editing configuration.yaml.

    Setting the level on our own logger is enough: Home Assistant's handlers sit
    on the root logger at NOTSET, so they pass on whatever reaches them.
    """
    _PACKAGE_LOGGER.setLevel(logging.DEBUG if debug else _CONFIGURED_LEVEL)

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
    command_rate: int
    debug_log: bool
    settle_timeout: float

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
            command_rate=int(o.get(CONF_COMMAND_RATE, DEFAULT_COMMAND_RATE)),
            debug_log=bool(o.get(CONF_DEBUG_LOG, DEFAULT_DEBUG_LOG)),
            settle_timeout=float(o.get(CONF_SETTLE_TIMEOUT, DEFAULT_SETTLE_TIMEOUT)),
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    definitions = HueDefinitions(hass)
    await definitions.async_refresh()

    options = Options.from_entry(entry)
    _apply_log_level(options.debug_log)

    watcher = Watcher(hass, definitions, options)
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

    # save_state / restore_state are global, not per config entry: they are
    # registered once and keep their snapshots when an entry reloads.
    await async_register_snapshots(hass, definitions)
    await async_register_flash(hass, definitions, watcher)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info(
        "Hue Insist active: %d attempts, %.1fs delay, max %d command(s)/s, "
        "bridge %s, verbose logging %s",
        watcher.options.retries,
        watcher.options.delay,
        watcher.options.command_rate,
        "reachable" if definitions.available else "unreachable",
        "on" if watcher.options.debug_log else "off",
    )
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply new options immediately, without a restart."""
    watcher: Watcher = hass.data[DOMAIN][entry.entry_id]
    watcher.options = Options.from_entry(entry)
    _apply_log_level(watcher.options.debug_log)
    await watcher.definitions.async_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok
