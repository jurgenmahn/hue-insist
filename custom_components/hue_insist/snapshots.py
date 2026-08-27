"""Named in-memory snapshots of light state.

A doorbell flash is the motivating case: `light.turn_on` with `flash: short` is
the only way to get a groupcast flash, but the call also switches every lamp on
and nothing switches them back. Taking a snapshot before the flash and restoring
it afterwards fixes that without giving up the single-message groupcast.

Two deliberate choices:

* Groups are expanded to their members. Restoring a Hue room or zone entity
  would push one aggregate state onto every lamp behind it, wiping the very
  per-lamp detail the snapshot exists to preserve. `group_members()` already
  knows the membership, so the expansion is free.

* Snapshots live in memory only. They exist to bridge a few seconds; surviving a
  restart is not worth the write amplification, and a stale snapshot restored
  hours later would be worse than none at all.

Restoring goes back out through `light.turn_on` / `light.turn_off`, so the
watcher picks it up and verifies the result per lamp like any other command.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.light import ATTR_BRIGHTNESS, DOMAIN as LIGHT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_SAVE_STATE = "save_state"
SERVICE_RESTORE_STATE = "restore_state"

ATTR_NAME = "name"
ATTR_CLEAR = "clear"

# Where the snapshots hang. Keyed off the domain bucket but deliberately not an
# entry_id, so unloading a config entry does not take them with it.
DATA_SNAPSHOTS = "_snapshots"

# Which colour attribute belongs to which colour mode. Sending the wrong one
# makes the lamp reinterpret the value -- an xy pair handed to a colour
# temperature lamp lands somewhere else entirely -- so only the attribute that
# matches the reported mode is captured.
_COLOUR_ATTR_BY_MODE = {
    "color_temp": "color_temp_kelvin",
    "hs": "hs_color",
    "xy": "xy_color",
    "rgb": "rgb_color",
    "rgbw": "rgbw_color",
    "rgbww": "rgbww_color",
}

SAVE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    }
)

RESTORE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_CLEAR, default=True): cv.boolean,
    }
)


def _expand(hass: HomeAssistant, definitions, entities: list[str]) -> list[str]:
    """Turn a mix of lamps and groups into a flat list of individual lamps."""
    lamps: list[str] = []
    for entity in entities:
        members = definitions.group_members(entity)
        lamps.extend(members or [entity])
    # dict.fromkeys keeps the order while dropping duplicates -- a lamp reached
    # through both its room and its zone would otherwise be captured twice.
    return list(dict.fromkeys(lamps))


def _all_individual_lights(hass: HomeAssistant, definitions) -> list[str]:
    """Every light entity that is not itself a group.

    Groups are skipped rather than expanded: their members are in the list
    already, and restoring the group as well would fight the per-lamp restore.
    """
    return [
        entity
        for entity in hass.states.async_entity_ids(LIGHT_DOMAIN)
        if not definitions.group_members(entity)
    ]


def _capture(hass: HomeAssistant, lamp: str) -> dict[str, Any] | None:
    """Read back what a lamp is doing right now, or None if it cannot be read."""
    state = hass.states.get(lamp)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None

    snap: dict[str, Any] = {"on": state.state == STATE_ON}
    if not snap["on"]:
        # An off lamp reports no brightness or colour, and restoring it only
        # needs the off itself.
        return snap

    if (brightness := state.attributes.get(ATTR_BRIGHTNESS)) is not None:
        snap[ATTR_BRIGHTNESS] = brightness

    mode = state.attributes.get("color_mode")
    if (attr := _COLOUR_ATTR_BY_MODE.get(mode)) is not None:
        if (value := state.attributes.get(attr)) is not None:
            snap[attr] = list(value) if isinstance(value, (list, tuple)) else value
    return snap


def _restore_batches(snapshot: dict[str, dict[str, Any]]) -> tuple[list[str], dict]:
    """Group lamps that need the same command, so the restore is not 33 calls.

    Lamps that were off collapse into a single turn_off. Lamps that were on are
    grouped by identical brightness and colour, which in practice covers whole
    rooms that were set from one scene.
    """
    off: list[str] = []
    on: dict[tuple, list[str]] = {}
    for lamp, snap in snapshot.items():
        if not snap.get("on"):
            off.append(lamp)
            continue
        key = tuple(sorted(
            (k, tuple(v) if isinstance(v, list) else v)
            for k, v in snap.items() if k != "on"
        ))
        on.setdefault(key, []).append(lamp)
    return off, on


async def async_register(hass: HomeAssistant, definitions) -> None:
    """Register save_state and restore_state once per Home Assistant."""
    store: dict[str, dict[str, dict[str, Any]]] = hass.data.setdefault(
        DOMAIN, {}
    ).setdefault(DATA_SNAPSHOTS, {})

    async def _save(call: ServiceCall) -> None:
        name = call.data[ATTR_NAME]
        requested = call.data.get(ATTR_ENTITY_ID)
        lamps = (
            _expand(hass, definitions, requested)
            if requested
            else _all_individual_lights(hass, definitions)
        )

        snapshot = {}
        skipped = 0
        for lamp in lamps:
            captured = _capture(hass, lamp)
            if captured is None:
                skipped += 1
                continue
            snapshot[lamp] = captured

        store[name] = snapshot
        _LOGGER.debug(
            "Snapshot '%s': %d lamp(s) captured, %d unreadable, %d on",
            name, len(snapshot), skipped,
            sum(1 for s in snapshot.values() if s.get("on")),
        )

    async def _restore(call: ServiceCall) -> None:
        name = call.data[ATTR_NAME]
        snapshot = store.get(name)
        if snapshot is None:
            _LOGGER.warning(
                "Snapshot '%s' does not exist -- nothing restored. Known: %s",
                name, ", ".join(sorted(store)) or "none",
            )
            return

        off, on = _restore_batches(snapshot)
        if off:
            await hass.services.async_call(
                LIGHT_DOMAIN, "turn_off", {ATTR_ENTITY_ID: off}, blocking=False
            )
        for key, lamps in on.items():
            data = {ATTR_ENTITY_ID: lamps}
            data.update({k: list(v) if isinstance(v, tuple) else v for k, v in key})
            await hass.services.async_call(
                LIGHT_DOMAIN, "turn_on", data, blocking=False
            )

        _LOGGER.debug(
            "Snapshot '%s' restored: %d lamp(s) off, %d group(s) on",
            name, len(off), len(on),
        )
        if call.data[ATTR_CLEAR]:
            store.pop(name, None)

    if not hass.services.has_service(DOMAIN, SERVICE_SAVE_STATE):
        hass.services.async_register(DOMAIN, SERVICE_SAVE_STATE, _save, SAVE_SCHEMA)
    if not hass.services.has_service(DOMAIN, SERVICE_RESTORE_STATE):
        hass.services.async_register(
            DOMAIN, SERVICE_RESTORE_STATE, _restore, RESTORE_SCHEMA
        )
