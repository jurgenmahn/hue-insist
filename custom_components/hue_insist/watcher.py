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

from homeassistant.components.light import (
    brightness_supported,
    color_supported,
    color_temp_supported,
)
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

# Above this the xy colours count as different. Roughly the point where the eye
# starts to notice on a white wall.
XY_TOLERANCE = 0.02

# How long the target lamps must stay quiet before the bridge counts as done,
# and how often to look. Both deliberately fixed: they describe how a Hue bridge
# behaves, not a preference anyone needs to tune.
SETTLE_WINDOW = 1.0
SETTLE_POLL = 0.25


def _describe(service: str, data: dict[str, Any]) -> str:
    """Render a light call the way it will actually be sent, for the log."""
    if service == "turn_off":
        return "off"
    parts = ["on"]
    if "brightness" in data:
        parts.append(f"bri {data['brightness']}")
    if "xy_color" in data:
        parts.append("xy {:.3f},{:.3f}".format(*data["xy_color"]))
    elif "color_temp_kelvin" in data:
        parts.append(f"{data['color_temp_kelvin']}K")
    return " ".join(parts)


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

    def to_service_data(
        self, modes: list[str] | None = None
    ) -> tuple[str, dict[str, Any]]:
        """What it takes to force this state onto a single lamp.

        Anything the lamp cannot do is left out. A scene sets a brightness for
        every member, including the smart plug in the corner, and sending that
        plug a brightness is meaningless.
        """
        if not self.on:
            return "turn_off", {}
        data: dict[str, Any] = {}
        if self.brightness is not None and (modes is None or brightness_supported(modes)):
            data["brightness"] = self.brightness
        if self.xy is not None and (modes is None or color_supported(modes)):
            data["xy_color"] = list(self.xy)
        elif self.kelvin is not None and (modes is None or color_temp_supported(modes)):
            data["color_temp_kelvin"] = self.kelvin
        return "turn_on", data

    def __str__(self) -> str:
        """Short form for the log."""
        return _describe(*self.to_service_data())


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
            # A flash is momentary: the caller wants a blink, not an end state.
            # Verifying it would read "everybody on" as the intent and switch on
            # every lamp that stayed dark -- turning a doorbell flash into a
            # house that stays lit. There is nothing to insist on here, because
            # the lamp is supposed to return to what it was doing.
            if "flash" in (data.get("service_data") or {}):
                _LOGGER.debug(
                    "Ignoring light.%s on %s -- flash is momentary, "
                    "the end state is not a target",
                    service,
                    ", ".join(self._entities_from(data.get("service_data", {}))) or "nothing",
                )
                return
            requested = self._entities_from(data.get("service_data", {}))
            targets = self._targets_for_light(service, data.get("service_data", {}), kinds)
            source = f"light.{service}"
        elif domain == "scene" and service == "turn_on" and self.options.watch_scenes:
            requested = self._entities_from(data.get("service_data", {}))
            targets = self._targets_for_scene(data.get("service_data", {}))
            source = "scene.turn_on"
            if targets:
                kinds.add("scene")
        else:
            return

        skipped = [e for e in targets if e in self.options.excluded]
        targets = {e: t for e, t in targets.items() if e not in self.options.excluded}
        if not targets:
            _LOGGER.debug(
                "Ignoring %s on %s -- nothing left to watch%s",
                source,
                ", ".join(requested) or "nothing",
                f" ({len(skipped)} excluded)" if skipped else "",
            )
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

        _LOGGER.debug(
            "Caught %s on %s [%s] -> %d lamp(s), target %s%s",
            source,
            ", ".join(requested) or "nothing",
            "+".join(sorted(kinds)) or "unknown",
            len(targets),
            next(iter(targets.values())),
            f", skipping {', '.join(skipped)}" if skipped else "",
        )

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
                _LOGGER.debug("Skipping group %s -- groups are not watched", entity)
                continue
            if not is_group and not self.options.watch_lights:
                _LOGGER.debug("Skipping lamp %s -- single lamps are not watched", entity)
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

            if is_group:
                _LOGGER.debug(
                    "%s expands to %d lamp(s): %s",
                    entity, len(members), ", ".join(sorted(members)),
                )
            for lamp in members or [entity]:
                targets[lamp] = target
        return targets

    def _targets_for_scene(self, data: dict[str, Any]) -> dict[str, Target]:
        targets: dict[str, Target] = {}
        for scene in self._entities_from(data):
            found = self.definitions.scene_targets(scene)
            if not found:
                _LOGGER.debug("Scene %s has no definition on the bridge", scene)
            for lamp, action in found.items():
                targets[lamp] = Target.from_hue_action(action)
        return targets

    async def _verify(self, job: Job) -> None:
        """Wait, compare, and correct whatever deviates."""
        corrected: set[str] = set()
        for attempt in range(1, self.options.retries + 1):
            await asyncio.sleep(self.options.delay)
            await self._settle(job)
            job.attempt = attempt

            deviations = {}
            for lamp, target in job.targets.items():
                reason = self._deviation(lamp, target)
                if reason:
                    deviations[lamp] = reason
            deviating = {lamp: job.targets[lamp] for lamp in deviations}

            if attempt == 1:
                # Counted once per request, not once per round: a single
                # stubborn lamp would otherwise dominate the totals.
                self.stats.devices_checked += len(job.targets)
                self.stats.devices_ok += len(job.targets) - len(deviating)
                self._publish()

            if not deviating:
                _LOGGER.debug(
                    "%s: all %d lamp(s) correct%s",
                    job.source, len(job.targets),
                    f" after attempt {attempt}" if attempt > 1 else "",
                )
                return

            _LOGGER.debug(
                "%s attempt %d/%d: %d of %d lamp(s) deviate",
                job.source, attempt, self.options.retries,
                len(deviating), len(job.targets),
            )
            for lamp, reason in sorted(deviations.items()):
                _LOGGER.debug("  %s: %s", lamp, reason)

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
            lamp for lamp, target in job.targets.items()
            if self._deviation(lamp, target) and not self._unverifiable(lamp)
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

    async def _settle(self, job: Job) -> None:
        """Hold off while the bridge is still working through the request.

        Turning off a whole house takes the bridge several seconds. Judging the
        result after a fixed two means correcting lamps whose turn had simply not
        come yet, which adds a pile of commands to the queue that is already the
        bottleneck -- the integration racing the bridge and losing.

        Rather than guess how long the bridge needs, watch it work: every lamp it
        reaches updates its state, so once those updates stop for a moment the
        request has been carried out as far as it is going to be. The timeout is
        there for the lamp that never stops changing.
        """
        if not self.options.settle_timeout:
            return

        started = self.hass.loop.time()
        deadline = started + self.options.settle_timeout
        while self.hass.loop.time() < deadline:
            latest = max(
                (
                    state.last_updated
                    for lamp in job.targets
                    if (state := self.hass.states.get(lamp)) is not None
                ),
                default=None,
            )
            if latest is None:
                return
            quiet = (dt_util.utcnow() - latest).total_seconds()
            if quiet >= SETTLE_WINDOW:
                break
            await asyncio.sleep(min(SETTLE_WINDOW - quiet, SETTLE_POLL))

        waited = self.hass.loop.time() - started
        if waited >= SETTLE_POLL:
            _LOGGER.debug(
                "Waited another %.1fs for the bridge to work through %d lamp(s)",
                waited, len(job.targets),
            )

    def _deviation(self, lamp: str, target: Target) -> str | None:
        """Say how a lamp differs from what was asked, or None when it matches.

        Returning the reason rather than a bare boolean is what makes the log
        worth reading: "off, expected on" and "brightness 140, expected 254" are
        different problems, and only the first is a missed command.
        """
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
            if self._unverifiable(lamp) and target.on:
                reported = state.state if state else "not in Home Assistant"
                return f"{reported}, correcting blind (cannot be verified)"
            return None

        actually_on = state.state == STATE_ON
        if actually_on != target.on:
            return f"{state.state}, expected {'on' if target.on else 'off'}"
        if not target.on:
            return None

        # Only hold a lamp to what it can actually do. A Hue smart plug reports
        # supported_color_modes ["onoff"]: it has no brightness at all, so
        # demanding one means it deviates on every single check, gets corrected
        # every round, and is reported as failed every time -- for doing exactly
        # what it was asked.
        attrs = state.attributes
        modes = attrs.get("supported_color_modes") or []

        if (
            self.options.check_brightness
            and target.brightness is not None
            and brightness_supported(modes)
        ):
            now = attrs.get("brightness")
            if now is None:
                return f"brightness unknown, expected {target.brightness}"
            if abs(int(now) - target.brightness) > self.options.brightness_tolerance:
                return f"brightness {int(now)}, expected {target.brightness}"

        if self.options.check_color:
            if target.xy is not None:
                now = attrs.get("xy_color")
                if now is not None:
                    if max(
                        abs(now[0] - target.xy[0]), abs(now[1] - target.xy[1])
                    ) > XY_TOLERANCE:
                        return (
                            f"xy {now[0]:.3f},{now[1]:.3f}, "
                            f"expected {target.xy[0]:.3f},{target.xy[1]:.3f}"
                        )
                elif color_supported(modes):
                    self._colour_unclear(
                        lamp, attrs,
                        f"xy {target.xy[0]:.3f},{target.xy[1]:.3f}",
                    )
            elif target.kelvin is not None:
                now = attrs.get("color_temp_kelvin")
                if now is not None:
                    # Compare in mired: equal steps there are visually equal, in
                    # kelvin they are not. 100K at 2000K is obvious, at 6000K it
                    # is imperceptible.
                    difference = abs(1_000_000 / int(now) - 1_000_000 / target.kelvin)
                    if difference > self.options.mirek_tolerance:
                        return (
                            f"{int(now)}K, expected {target.kelvin}K "
                            f"({difference:.0f} mired off)"
                        )
                elif color_temp_supported(modes):
                    self._colour_unclear(lamp, attrs, f"{target.kelvin}K")
        return None

    def _colour_unclear(self, lamp: str, attrs, wanted: str) -> None:
        """Note that the lamp answered in a different colour mode than was asked.

        Deliberately not a deviation. A light reports only the attribute for the
        colour mode it is currently in, so a lamp sitting in xy has no colour
        temperature to compare against -- and converting between the two loses
        far too much to judge on. Home Assistant's own round trip through xy
        comes back over a hundred mired out at warm white, well past any sane
        tolerance, which would condemn a lamp that is doing exactly what it was
        told and never stop correcting it.

        So report the mismatch and leave the verdict to a human. Third-party
        Zigbee bulbs joined to a Hue bridge are the usual cause: the bridge
        resolves the requested colour temperature to xy for that lamp's gamut,
        and xy is what comes back.
        """
        if attrs.get("xy_color"):
            reports = "xy {:.3f},{:.3f}".format(*attrs["xy_color"])
        elif attrs.get("color_temp_kelvin"):
            reports = f"{attrs['color_temp_kelvin']}K"
        else:
            reports = "no colour at all"
        _LOGGER.debug(
            "  %s: colour not comparable -- asked for %s, lamp reports %s "
            "(colour mode %s); not counted as a deviation",
            lamp, wanted, reports, attrs.get("color_mode") or "unknown",
        )

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
        """Address each lamp on its own, paced to what the bridge can take.

        One at a time rather than as a group: a unicast command is acknowledged
        by the Zigbee stack and retried when needed, a groupcast is not. That
        difference is the entire point of this integration.

        The pacing matters just as much. The Hue bridge handles roughly ten light
        commands per second and silently drops whatever arrives on top of that --
        no error, no retry. Firing thirty corrections at once therefore repairs
        almost nothing and makes the congestion worse; spreading them out repairs
        all thirty, only slower.
        """
        interval = 1 / self.options.command_rate if self.options.command_rate else 0
        started = self.hass.loop.time()

        for index, (lamp, target) in enumerate(deviating.items()):
            if index and interval:
                await asyncio.sleep(interval)
            state = self.hass.states.get(lamp)
            modes = state.attributes.get("supported_color_modes") if state else None
            service, data = target.to_service_data(modes)
            context = Context()
            self._own_contexts.add(context.id)
            if len(self._own_contexts) > 500:
                self._own_contexts = set(list(self._own_contexts)[-250:])
            _LOGGER.debug("  -> light.%s %s (%s)", service, lamp, _describe(service, data))
            await self.hass.services.async_call(
                "light", service, {ATTR_ENTITY_ID: lamp, **data},
                blocking=False, context=context,
            )

        spent = self.hass.loop.time() - started
        if spent > self.options.delay:
            _LOGGER.debug(
                "Correcting %d lamp(s) took %.1fs at %d/s, longer than the %.1fs "
                "delay; the next check simply shifts along",
                len(deviating), spent, self.options.command_rate, self.options.delay,
            )
