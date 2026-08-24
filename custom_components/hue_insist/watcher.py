"""Catches light commands, verifies the outcome and repairs what went wrong.

The problem this solves: a Hue group or scene is sent by the bridge as a Zigbee
groupcast. Groupcast is not acknowledged per lamp and is therefore never retried
at the Zigbee level. A bulb with marginal range misses the message for good, and
nobody notices -- Home Assistant reports the group as on the moment one of its
members lights up.

The approach: every request that passes through Home Assistant is caught,
translated into a concrete target state per lamp, and verified after a short
pause. Whatever does not match is corrected lamp by lamp -- unicast, so with
acknowledgement and with retries by the Zigbee stack itself.

Deliberately out of scope: control straight from the Hue app. That never passes
through Home Assistant and is therefore invisible.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    EVENT_CORRECTED,
    EVENT_FAILED,
    SIGNAL_STATS_UPDATED,
    HA_BRIGHTNESS_MAX,
    HUE_BRIGHTNESS_MAX,
)
from .hue_api import HueDefinitions
from .stats import Stats

_LOGGER = logging.getLogger(__name__)

UNKNOWN_STATES = (None, "unknown", "unavailable")


@dataclass
class Target:
    """The intended end state of a single lamp."""

    on: bool
    brightness: int | None = None      # 0-255, the way Home Assistant reports it
    kelvin: int | None = None
    xy: tuple[float, float] | None = None

    @classmethod
    def from_hue_action(cls, action: dict[str, Any]) -> "Target":
        """Translate a scene action from the bridge into a target state."""
        on = bool(action.get("on", {}).get("on", True))
        brightness = None
        if "dimming" in action:
            pct = float(action["dimming"].get("brightness", 0))
            brightness = round(pct / HUE_BRIGHTNESS_MAX * HA_BRIGHTNESS_MAX)
        kelvin = None
        if "color_temperature" in action:
            mirek = action["color_temperature"].get("mirek")
            if mirek:
                kelvin = round(1_000_000 / int(mirek))
        xy = None
        if "color" in action and "xy" in action["color"]:
            point = action["color"]["xy"]
            xy = (float(point["x"]), float(point["y"]))
        return cls(on=on, brightness=brightness, kelvin=kelvin, xy=xy)

    @classmethod
    def from_service_data(cls, service: str, data: dict[str, Any]) -> "Target":
        """Translate a light.turn_on/turn_off call into a target state."""
        if service == "turn_off":
            return cls(on=False)

        brightness = data.get("brightness")
        if brightness is None and "brightness_pct" in data:
            brightness = round(float(data["brightness_pct"]) / 100 * HA_BRIGHTNESS_MAX)
        kelvin = data.get("color_temp_kelvin")
        if kelvin is None and data.get("color_temp"):
            kelvin = round(1_000_000 / int(data["color_temp"]))
        xy = tuple(data["xy_color"]) if data.get("xy_color") else None
        return cls(on=True, brightness=brightness, kelvin=kelvin, xy=xy)

    def to_service_data(self) -> tuple[str, dict[str, Any]]:
        """What it takes to force this state onto a single lamp."""
        if not self.on:
            return "turn_off", {}
        data: dict[str, Any] = {}
        if self.brightness is not None:
            data["brightness"] = self.brightness
        if self.xy is not None:
            data["xy_color"] = list(self.xy)
        elif self.kelvin is not None:
            data["color_temp_kelvin"] = self.kelvin
        return "turn_on", data


@dataclass
class Job:
    """A caught request, waiting to be verified."""

    targets: dict[str, Target]
    attempt: int = 0
    source: str = ""
    failed: list[str] = field(default_factory=list)


class Watcher:
    """Listens, verifies and corrects."""

    def __init__(self, hass: HomeAssistant, definitions: HueDefinitions, options) -> None:
        self.hass = hass
        self.definitions = definitions
        self.options = options
        self._own_contexts: set[str] = set()
        self.stats = Stats()

    @callback
    def handle_event(self, event: Event) -> None:
        """Judge a call_service event and schedule a check when relevant."""
        data = event.data
        domain = data.get("domain")
        service = data.get("service")

        # Our own corrections must not trigger another check; that would be an
        # endless loop. The context we call with is known to us.
        if event.context and event.context.id in self._own_contexts:
            return

        kinds: set[str] = set()
        if domain == "light" and service in ("turn_on", "turn_off", "toggle"):
            targets = self._targets_for_light(service, data.get("service_data", {}), kinds)
            source = f"light.{service}"
        elif domain == "scene" and service == "turn_on" and self.options.watch_scenes:
            targets = self._targets_for_scene(data.get("service_data", {}))
            source = "scene.turn_on"
            if targets:
                kinds.add("scene")
        else:
            return

        targets = {e: t for e, t in targets.items() if e not in self.options.excluded}
        if not targets:
            return

        self.stats.actions_total += 1
        self.stats.last_action = dt_util.utcnow()
        if "scene" in kinds:
            self.stats.actions_scene += 1
        if "group" in kinds:
            self.stats.actions_group += 1
        if "device" in kinds:
            self.stats.actions_device += 1
        self._publish()

        self.hass.async_create_task(
            self._verify(Job(targets=targets, source=source))
        )

    def _entities_from(self, service_data: dict[str, Any]) -> list[str]:
        raw = service_data.get(ATTR_ENTITY_ID) or []
        return [raw] if isinstance(raw, str) else list(raw)

    def _targets_for_light(
        self, service: str, data: dict[str, Any], kinds: set[str]
    ) -> dict[str, Target]:
        targets: dict[str, Target] = {}
        for entity in self._entities_from(data):
            members = self.definitions.group_members(entity)
            is_group = bool(members)

            if is_group and not self.options.watch_groups:
                continue
            if not is_group and not self.options.watch_lights:
                continue
            kinds.add("group" if is_group else "device")

            if service == "toggle":
                # The intended end state depends on the state right now, so it
                # has to be captured here -- after the delay it is gone.
                current = self.hass.states.get(entity)
                will_be_on = not (current and current.state == STATE_ON)
                target = Target(on=will_be_on)
            else:
                target = Target.from_service_data(service, data)

            for lamp in members or [entity]:
                targets[lamp] = target
        return targets

    def _targets_for_scene(self, data: dict[str, Any]) -> dict[str, Target]:
        targets: dict[str, Target] = {}
        for scene in self._entities_from(data):
            for lamp, action in self.definitions.scene_targets(scene).items():
                targets[lamp] = Target.from_hue_action(action)
        return targets

    async def _verify(self, job: Job) -> None:
        """Wait, compare, and correct whatever deviates."""
        corrected: set[str] = set()
        for attempt in range(1, self.options.retries + 1):
            await asyncio.sleep(self.options.delay)
            job.attempt = attempt

            deviating = {
                lamp: target
                for lamp, target in job.targets.items()
                if self._deviates(lamp, target)
            }
            if attempt == 1:
                # Counted once per request, not once per round: a single
                # stubborn lamp would otherwise dominate the totals.
                self.stats.devices_checked += len(job.targets)
                self.stats.devices_ok += len(job.targets) - len(deviating)
                self._publish()

            if not deviating:
                if attempt > 1:
                    _LOGGER.debug("All correct after attempt %d", attempt)
                return

            _LOGGER.debug(
                "Attempt %d: %d lamp(s) deviate: %s",
                attempt, len(deviating), ", ".join(deviating),
            )
            await self._correct(deviating)

            # Count each lamp once per request, not once per retry round. A lamp
            # that needs three attempts is still one lamp that needed nudging;
            # counting per round multiplies it by the retry count and pushes the
            # total above the number of lamps checked, which is impossible on its
            # face and makes the ratio worthless.
            fresh = deviating.keys() - corrected
            corrected |= deviating.keys()
            self.stats.corrections += len(fresh)
            self.stats.last_correction = dt_util.utcnow()
            self._publish()
            self.hass.bus.async_fire(
                EVENT_CORRECTED,
                {"entities": list(deviating), "attempt": attempt, "source": job.source},
            )

        # Still wrong after the last attempt: give up and report.
        # Lamps we cannot verify are excluded from the tally: reporting them as
        # failures every single time would drown out the real ones.
        remaining = [
            l for l, t in job.targets.items()
            if self._deviates(l, t) and not self._unverifiable(l)
        ]
        if remaining:
            self.stats.failures += len(remaining)
            self.stats.last_failure = dt_util.utcnow()
            self.stats.last_failed_entities = remaining
            self._publish()
            _LOGGER.warning(
                "Gave up after %d attempts: %s (triggered by: %s)",
                self.options.retries, ", ".join(remaining), job.source,
            )
            self.hass.bus.async_fire(
                EVENT_FAILED,
                {"entities": remaining, "attempts": self.options.retries, "source": job.source},
            )

    def _deviates(self, lamp: str, target: Target) -> bool:
        """Decide whether a lamp is not what was asked for."""
        state = self.hass.states.get(lamp)
        if state is None or state.state in UNKNOWN_STATES:
            # Unreachable lamps are normally left out: a bulb without power --
            # one behind a cupboard door switch, say -- would otherwise be
            # retried every round and fail every time.
            #
            # The exception list turns that around for lamps whose state cannot
            # be trusted but which still need the command. A Hue lamp used as a
            # proxy for non-Hue hardware is the case this was built for: it
            # reports unavailable, yet the command has to reach it. Those are
            # treated as deviating so a correction is sent, and they are left
            # out of the failure tally because there is nothing to verify.
            return self._unverifiable(lamp) and target.on

        actually_on = state.state == STATE_ON
        if actually_on != target.on:
            return True
        if not target.on:
            return False

        attrs = state.attributes
        if self.options.check_brightness and target.brightness is not None:
            now = attrs.get("brightness")
            if now is None or abs(int(now) - target.brightness) > self.options.brightness_tolerance:
                return True

        if self.options.check_color:
            if target.xy is not None:
                now = attrs.get("xy_color")
                if now is None or max(
                    abs(now[0] - target.xy[0]), abs(now[1] - target.xy[1])
                ) > 0.02:
                    return True
            elif target.kelvin is not None:
                now = attrs.get("color_temp_kelvin")
                if now is None:
                    return True
                # Compare in mired: equal steps there are visually equal, in
                # kelvin they are not. 100K at 2000K is obvious, at 6000K it is
                # imperceptible.
                difference = abs(1_000_000 / int(now) - 1_000_000 / target.kelvin)
                if difference > self.options.mirek_tolerance:
                    return True
        return False

    @callback
    def _publish(self) -> None:
        """Tell the diagnostic sensors their numbers moved."""
        async_dispatcher_send(self.hass, SIGNAL_STATS_UPDATED)

    def _unverifiable(self, lamp: str) -> bool:
        """Is this a lamp we correct blindly, without being able to check?"""
        if not self.options.skip_unavailable:
            return True
        return lamp in self.options.unavailable_exceptions

    async def _correct(self, deviating: dict[str, Target]) -> None:
        """Address each lamp on its own.

        One at a time rather than as a group: a unicast command is acknowledged
        by the Zigbee stack and retried when needed, a groupcast is not. That
        difference is the entire point of this integration.
        """
        for lamp, target in deviating.items():
            service, data = target.to_service_data()
            context = Context()
            self._own_contexts.add(context.id)
            if len(self._own_contexts) > 500:
                self._own_contexts = set(list(self._own_contexts)[-250:])
            await self.hass.services.async_call(
                "light", service, {ATTR_ENTITY_ID: lamp, **data},
                blocking=False, context=context,
            )
