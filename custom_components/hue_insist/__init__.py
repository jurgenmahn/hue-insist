"""Hue Insist -- zorgt dat een lichtcommando ook echt uitgevoerd wordt.

Een Hue-groep of scene gaat als Zigbee groupcast de lucht in. Groupcast wordt niet
per lamp bevestigd en dus ook niet herhaald: een lamp met matig bereik mist het
bericht definitief, en Home Assistant merkt daar niets van omdat de groep al als
"aan" telt zodra een van de leden brandt.

Deze integratie vangt elk lichtverzoek dat via Home Assistant loopt, vertaalt het
naar een concrete eindstand per lamp -- voor scenes rechtstreeks uit de definitie
op de bridge, inclusief helderheid en kleur -- controleert na een korte pauze wat
er werkelijk gebeurd is, en corrigeert afwijkende lampen stuk voor stuk. Die
losse commando's gaan als unicast en worden wel bevestigd.
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
    CONF_WATCH_GROUPS,
    CONF_WATCH_LIGHTS,
    CONF_WATCH_SCENES,
    DEFAULT_BRIGHTNESS_TOLERANCE,
    DEFAULT_DELAY,
    DEFAULT_MIREK_TOLERANCE,
    DEFAULT_RETRIES,
    DOMAIN,
)
from .hue_api import HueDefinitions
from .watcher import Watcher

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]

# De bridge wordt periodiek opnieuw uitgelezen zodat een nieuwe of gewijzigde
# scene vanzelf meeloopt, zonder herstart.
VERVERS_INTERVAL = timedelta(minutes=30)


@dataclass
class Opties:
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

    @classmethod
    def van_entry(cls, entry: ConfigEntry) -> "Opties":
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
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    definities = HueDefinitions(hass)
    await definities.async_refresh()

    watcher = Watcher(hass, definities, Opties.van_entry(entry))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = watcher

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_CALL_SERVICE, watcher.behandel_event)
    )

    async def _ververs(_now) -> None:
        await definities.async_refresh()

    entry.async_on_unload(
        async_track_time_interval(hass, _ververs, VERVERS_INTERVAL)
    )
    entry.async_on_unload(entry.add_update_listener(_opties_gewijzigd))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info(
        "Hue Insist actief: %d pogingen, %.1fs tussenpauze, bridge %s",
        watcher.opties.retries,
        watcher.opties.delay,
        "bereikbaar" if definities.available else "niet bereikbaar",
    )
    return True


async def _opties_gewijzigd(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Nieuwe opties meteen laten gelden, zonder herstart."""
    watcher: Watcher = hass.data[DOMAIN][entry.entry_id]
    watcher.opties = Opties.van_entry(entry)
    await watcher.definities.async_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok
