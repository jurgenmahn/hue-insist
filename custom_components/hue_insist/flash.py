"""A flash you actually notice.

`light.turn_on` with `flash: short` is one groupcast, so it is fast, but it
blinks every lamp once and simultaneously. In a house that means a single blink
per room -- easy to miss if you happen to be looking the other way -- and it
leaves every lamp switched on afterwards.

This service pulses instead: on, off, on, off, for as long as you ask, over a
chosen number of lamps at a time. Picking a random subset per pulse makes the
house ripple rather than strobe, which is both easier to notice and far kinder
to the bridge.

The bridge is the real constraint. It handles roughly ten light commands per
second and silently drops whatever lands on top of that -- no error, no retry.
A pulse over N lamps costs N commands, and every pulse has an on and an off, so
the arithmetic runs away quickly. Rather than promise a cadence the bridge
cannot deliver, the service works out what was asked for, logs a warning when
that exceeds the budget, and carries on at the requested timing so the caller
can see for themselves what does and does not arrive.

One exception is worth the special case: flashing a Hue room or zone as a whole
is a groupcast, one command no matter how many lamps hang behind it. When the
target is a group and every lamp is meant to pulse together, the group entity is
used directly and the budget stops mattering.

Nothing here is verified or retried. A missed blink is a missed blink; insisting
on it would leave the lamp switched on, which is the opposite of what a flash is
for.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import voluptuous as vol

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.service import async_extract_entity_ids

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_FLASH_LIGHTS = "flash_lights"

ATTR_FLASH_DURATION = "flash_duration"
ATTR_ON_DURATION = "on_duration"
ATTR_OFF_DURATION = "off_duration"
ATTR_CONCURRENT = "concurrent_lights"
ATTR_BRIGHTNESS = "brightness"

# Below this a pulse stops being a blink and becomes a flicker, and the lamp's
# own fade swallows most of it. Kept low deliberately: what the bridge actually
# delivers is the real limit, and that is reported separately rather than
# pretended away by a schema bound.
_MIN_PULSE_MS = 20

FLASH_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_FLASH_DURATION, default=3000): vol.All(int, vol.Range(min=100, max=60000)),
        vol.Optional(ATTR_ON_DURATION, default=250): vol.All(int, vol.Range(min=_MIN_PULSE_MS, max=10000)),
        vol.Optional(ATTR_OFF_DURATION, default=250): vol.All(int, vol.Range(min=_MIN_PULSE_MS, max=10000)),
        vol.Optional(ATTR_CONCURRENT, default=0): vol.All(int, vol.Range(min=0)),
        vol.Optional(ATTR_BRIGHTNESS): vol.All(int, vol.Range(min=1, max=255)),
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional("device_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("area_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("label_id"): vol.Any(cv.string, [cv.string]),
    }
)


async def _resolve(hass: HomeAssistant, definitions, call: ServiceCall) -> tuple[list[str], str | None]:
    """Work out which lamps to pulse.

    Returns the individual lamps plus, when the whole request happens to be a
    single Hue group, that group entity -- the caller can then use it for a
    groupcast instead of addressing every member.
    """
    # expand_group=False: a Hue group must stay recognisable as a group here,
    # because flashing it as one costs a single command instead of one per lamp.
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


def _budget_warning(
    lamps_per_pulse: int, on_ms: int, off_ms: int, rate: int
) -> str | None:
    """Compare what was asked for against what the bridge can carry."""
    if not rate:
        return None
    cycle_s = (on_ms + off_ms) / 1000
    if cycle_s <= 0:
        return None
    needed = (lamps_per_pulse * 2) / cycle_s
    if needed <= rate:
        return None
    return (
        f"{needed:.0f} command(s)/s needed but the bridge takes about {rate}; "
        f"lower concurrent_lights or lengthen on_duration/off_duration, "
        f"or blinks will be dropped"
    )


async def async_register(hass: HomeAssistant, definitions, watcher) -> None:
    """Register flash_lights once per Home Assistant."""

    def _context() -> Context:
        """A context the watcher recognises as ours, so it stays out of this."""
        context = Context()
        watcher._own_contexts.add(context.id)
        if len(watcher._own_contexts) > 500:
            watcher._own_contexts = set(list(watcher._own_contexts)[-250:])
        return context

    async def _flash(call: ServiceCall) -> None:
        lamps, group_entity = await _resolve(hass, definitions, call)
        if not lamps:
            _LOGGER.warning("flash_lights: nothing to flash for this target")
            return

        total_ms = call.data[ATTR_FLASH_DURATION]
        on_ms = call.data[ATTR_ON_DURATION]
        off_ms = call.data[ATTR_OFF_DURATION]
        concurrent = call.data[ATTR_CONCURRENT] or len(lamps)
        concurrent = min(concurrent, len(lamps))
        brightness = call.data.get(ATTR_BRIGHTNESS)

        # Whole set at once and the target was one group: use the group entity,
        # which costs a single command per pulse instead of one per lamp.
        as_group = group_entity is not None and concurrent >= len(lamps)
        per_pulse = 1 if as_group else concurrent

        if (warning := _budget_warning(per_pulse, on_ms, off_ms, watcher.options.command_rate)):
            _LOGGER.warning("flash_lights: %s", warning)

        on_data: dict[str, Any] = {"transition": 0}
        if brightness is not None:
            on_data[ATTR_BRIGHTNESS] = brightness

        _LOGGER.debug(
            "flash_lights: %d lamp(s), %d at a time%s, %dms on / %dms off, for %dms",
            len(lamps), concurrent, " as one groupcast" if as_group else "",
            on_ms, off_ms, total_ms,
        )

        deadline = hass.loop.time() + total_ms / 1000
        pulses = 0
        while hass.loop.time() < deadline:
            if as_group:
                picked = [group_entity]
            elif concurrent >= len(lamps):
                picked = lamps
            else:
                # A fresh draw per pulse, so the flash travels through the house
                # instead of hammering the same handful of lamps.
                picked = random.sample(lamps, concurrent)

            await hass.services.async_call(
                LIGHT_DOMAIN, "turn_on", {ATTR_ENTITY_ID: picked, **on_data},
                blocking=False, context=_context(),
            )
            await asyncio.sleep(on_ms / 1000)
            await hass.services.async_call(
                LIGHT_DOMAIN, "turn_off", {ATTR_ENTITY_ID: picked, "transition": 0},
                blocking=False, context=_context(),
            )
            await asyncio.sleep(off_ms / 1000)
            pulses += 1

        _LOGGER.debug("flash_lights: %d pulse(s) sent", pulses)

    if not hass.services.has_service(DOMAIN, SERVICE_FLASH_LIGHTS):
        hass.services.async_register(
            DOMAIN, SERVICE_FLASH_LIGHTS, _flash, FLASH_SCHEMA
        )
