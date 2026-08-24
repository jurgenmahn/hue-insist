"""Reads scene and group definitions from the Hue bridge.

Why go to the bridge instead of using Home Assistant's own state: only the bridge
knows what a scene *means*. Home Assistant knows a scene's name and which entities
belong to it, but not the brightness or colour each lamp is supposed to end up at.
The bridge gives exactly that, per lamp:

    {"on": {"on": true}, "dimming": {"brightness": 9.49},
     "color_temperature": {"mirek": 357}}

That makes it possible to check whether a lamp really became what was asked for,
instead of merely checking that it is on.

Linking bridge to Home Assistant is free: the Hue integration uses the bridge's
resource id directly as the entity's unique_id. A lookup in the entity registry is
enough, no heuristics needed.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

HUE_DOMAIN = "hue"


class HueDefinitions:
    """Cache of scene, room and zone definitions from the Hue bridge."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._host: str | None = None
        self._key: str | None = None
        self._scenes: dict[str, list[dict[str, Any]]] = {}
        self._groups: dict[str, list[str]] = {}
        self._rid_to_entity: dict[str, str] = {}
        self.available = False

    def _credentials(self) -> tuple[str, str] | None:
        """Reuse the credentials of the existing Hue integration.

        Pairing separately would mean the user has to press the bridge button
        again and that a second application key needs managing. The existing
        config entry already has everything required.
        """
        for entry in self.hass.config_entries.async_entries(HUE_DOMAIN):
            host = entry.data.get("host")
            key = entry.data.get("api_key")
            if host and key:
                return host, key
        return None

    async def async_refresh(self) -> None:
        """Fetch scenes and groups, and rebuild the translation table."""
        creds = self._credentials()
        if creds is None:
            self.available = False
            _LOGGER.debug("No Hue integration found; scenes will not be expanded")
            return
        self._host, self._key = creds

        try:
            scenes = await self._get("scene")
            rooms = await self._get("room")
            zones = await self._get("zone")
            devices = await self._get("device")
        except (aiohttp.ClientError, TimeoutError) as err:
            self.available = False
            _LOGGER.warning("Could not query the Hue bridge: %s", err)
            return

        self._scenes = {s["id"]: s.get("actions", []) for s in scenes}

        # A room or zone points through "children" at DEVICES, not at lights.
        # Home Assistant uses the rid of the light SERVICE as unique_id, so an
        # extra hop is required: device -> light service. Without it every lookup
        # comes up empty, because a device rid never appears in the registry.
        device_to_light: dict[str, str] = {}
        for dev in devices:
            for svc in dev.get("services", []):
                if svc.get("rtype") == "light":
                    device_to_light[dev["id"]] = svc["rid"]
                    break

        # The grouped_light service of a room or zone is what Home Assistant
        # surfaces as the group entity, so that rid is the key.
        self._groups = {}
        for grp in rooms + zones:
            lights = [
                device_to_light[c["rid"]]
                for c in grp.get("children", [])
                if c["rid"] in device_to_light
            ]
            for svc in grp.get("services", []):
                if svc.get("rtype") == "grouped_light":
                    self._groups[svc["rid"]] = lights

        self._build_entity_map()
        self.available = True
        _LOGGER.debug(
            "Hue definitions refreshed: %d scenes, %d groups, %d entities linked",
            len(self._scenes),
            len(self._groups),
            len(self._rid_to_entity),
        )

    async def _get(self, resource: str) -> list[dict[str, Any]]:
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{self._host}/clip/v2/resource/{resource}"
        async with session.get(
            url,
            headers={"hue-application-key": self._key},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        return payload.get("data", [])

    def _build_entity_map(self) -> None:
        """Link Hue resource ids to Home Assistant entities."""
        registry = er.async_get(self.hass)
        self._rid_to_entity = {
            entry.unique_id: entry.entity_id
            for entry in registry.entities.values()
            if entry.platform == HUE_DOMAIN and entry.unique_id
        }

    def entity_for(self, rid: str) -> str | None:
        return self._rid_to_entity.get(rid)

    def scene_targets(self, scene_entity: str) -> dict[str, dict[str, Any]]:
        """Return the intended end state per lamp entity for a scene."""
        registry = er.async_get(self.hass)
        entry = registry.async_get(scene_entity)
        if entry is None or entry.unique_id not in self._scenes:
            return {}

        targets: dict[str, dict[str, Any]] = {}
        for action in self._scenes[entry.unique_id]:
            lamp = self.entity_for(action.get("target", {}).get("rid", ""))
            if lamp:
                targets[lamp] = action.get("action", {})
        return targets

    def group_members(self, group_entity: str) -> list[str]:
        """Return the lamp entities behind a Hue room or zone entity.

        Falls back to the entity_id attribute the Hue integration puts on the
        group entity itself, so this keeps working when the bridge is
        unreachable.
        """
        registry = er.async_get(self.hass)
        entry = registry.async_get(group_entity)
        if entry and entry.unique_id in self._groups:
            members = [self.entity_for(rid) for rid in self._groups[entry.unique_id]]
            found = [x for x in members if x]
            if found:
                return found

        state = self.hass.states.get(group_entity)
        if state:
            return list(state.attributes.get("entity_id") or [])
        return []
