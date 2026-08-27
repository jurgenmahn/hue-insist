"""A flash you actually notice, run by the bridge rather than by us.

The Hue v2 API has a signalling mechanism that takes a duration: hand it
`on_off` and a number of milliseconds and the bridge blinks the target for
exactly that long, entirely on its own. On a room or zone that is a single
request for the whole group, so every lamp is in step by construction.

That is worth spelling out, because getting here took three worse attempts and
each failed for a reason that is invisible from Home Assistant:

* Sending on/off pairs ourselves costs two commands per blink per lamp. The
  bridge takes roughly ten light commands a second and silently drops the rest,
  so a house-wide flash asks for several times what can arrive, and the result
  is ragged.
* `light.turn_on` with `flash: short` looks like a groupcast but is not: aiohue
  expands it into one call per member light, which arrive one by one over the
  better part of a second. Hence lamps blinking out of step.
* `flash: long` does reach the group as one command, but starts a breathe that
  runs about fifteen seconds on the lamp's own clock and cannot be stopped by
  any ordinary state command. A three second flash lasted nineteen.

None of those are fixable from this side, because the timing that matters
happens after the command leaves. Signalling moves the whole sequence to where
the lamps are, which is the only place it can be kept in time.

What that costs: the rhythm of the blinking is the bridge's choice and cannot
be set, and the duration has a step size of one second.
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.service import async_extract_entity_ids

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_FLASH_LIGHTS = "flash_lights"

ATTR_FLASH_DURATION = "flash_duration"

# Toggles between full brightness and off, in the colour the lamp already has.
_SIGNAL_ON_OFF = "on_off"

# The bridge rounds the duration to whole seconds, so a request below one second
# would round away to nothing. Better to refuse it than to flash unpredictably.
FLASH_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_FLASH_DURATION, default=3000): vol.All(
            int, vol.Range(min=1000, max=60000)
        ),
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional("device_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("area_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("label_id"): vol.Any(cv.string, [cv.string]),
    }
)


async def _resolve(
    hass: HomeAssistant, definitions, call: ServiceCall
) -> tuple[list[str], str | None]:
    """Work out what to signal.

    Returns the individual lamps plus, when the whole request happens to be a
    single Hue group, that group entity -- signalling the group is one request
    instead of one per lamp, and only the group keeps them in step.
    """
    # expand_group=False: a Hue group must stay recognisable as a group here,
    # because that is precisely what makes the flash synchronous.
    referenced = list(await async_extract_entity_ids(call, expand_group=False))

    group_entity: str | None = None
    lamps: list[str] = []
    for entity in referenced:
        if entity.startswith("scene."):
            # A scene names its lamps in its own definition; the bridge knows
            # them even when the scene entity itself says nothing useful.
            lamps.extend(definitions.scene_targets(entity).keys())
            continue
        if not entity.startswith("light."):
            continue
        members = definitions.group_members(entity)
        if members:
            if len(referenced) == 1:
                group_entity = entity
            lamps.extend(members)
        else:
            lamps.append(entity)

    return list(dict.fromkeys(lamps)), group_entity


async def async_register(hass: HomeAssistant, definitions) -> None:
    """Register flash_lights once per Home Assistant."""

    async def _flash(call: ServiceCall) -> None:
        lamps, group_entity = await _resolve(hass, definitions, call)
        if not lamps:
            _LOGGER.warning("flash_lights: nothing to flash for this target")
            return

        total_ms = call.data[ATTR_FLASH_DURATION]
        targets = [group_entity] if group_entity else lamps

        results = await asyncio.gather(
            *[
                definitions.async_signal(target, _SIGNAL_ON_OFF, total_ms)
                for target in targets
            ]
        )

        _LOGGER.debug(
            "flash_lights: %dms on %s (%d lamp(s)), %d of %d accepted",
            total_ms,
            "the group as one" if group_entity else "each lamp separately",
            len(lamps),
            sum(results),
            len(targets),
        )

        # The request returns the moment the bridge accepts it, but the flash it
        # starts runs for the full duration. Returning here would let whatever
        # comes next -- a restore_state, typically -- land in the middle of it.
        # Waiting keeps "flash for three seconds" meaning what it says, so a
        # caller can simply put the next step after it.
        if any(results):
            await asyncio.sleep(total_ms / 1000)

    if not hass.services.has_service(DOMAIN, SERVICE_FLASH_LIGHTS):
        hass.services.async_register(
            DOMAIN, SERVICE_FLASH_LIGHTS, _flash, FLASH_SCHEMA
        )
