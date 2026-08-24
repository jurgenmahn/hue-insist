"""Leest scene- en groepsdefinities uit de Hue-bridge.

Waarom rechtstreeks de bridge en niet de toestand in Home Assistant: alleen de
bridge weet wat een scene *bedoelt*. Home Assistant kent van een scene enkel de
naam en welke entiteiten erin zitten, niet op welke helderheid of kleur elke lamp
zou moeten staan. De bridge geeft dat per lamp:

    {"on": {"on": true}, "dimming": {"brightness": 9.49},
     "color_temperature": {"mirek": 357}}

Daarmee is te controleren of een lamp werkelijk geworden is wat er gevraagd werd,
in plaats van alleen of hij aan is.

De koppeling tussen bridge en Home Assistant is gratis: de Hue-integratie gebruikt
de resource-id van de bridge rechtstreeks als unique_id van de entiteit. Een
opzoeking in het entiteitenregister is dus genoeg, er is geen heuristiek nodig.
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
    """Cache van scene-, kamer- en zonedefinities van de Hue-bridge."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._host: str | None = None
        self._key: str | None = None
        self._scenes: dict[str, list[dict[str, Any]]] = {}
        self._groups: dict[str, list[str]] = {}
        self._rid_to_entity: dict[str, str] = {}
        self.available = False

    def _credentials(self) -> tuple[str, str] | None:
        """Hergebruik de gegevens van de bestaande Hue-integratie.

        Een eigen koppeling met de bridge zou betekenen dat de gebruiker opnieuw
        op de knop moet drukken en dat er een tweede sleutel beheerd wordt. De
        bestaande config entry heeft alles wat nodig is.
        """
        for entry in self.hass.config_entries.async_entries(HUE_DOMAIN):
            host = entry.data.get("host")
            key = entry.data.get("api_key")
            if host and key:
                return host, key
        return None

    async def async_refresh(self) -> None:
        """Haal scenes en groepen op en bouw de vertaaltabel opnieuw."""
        creds = self._credentials()
        if creds is None:
            self.available = False
            _LOGGER.debug("Geen Hue-integratie gevonden; scenes worden niet uitgeklapt")
            return
        self._host, self._key = creds

        try:
            scenes = await self._get("scene")
            rooms = await self._get("room")
            zones = await self._get("zone")
            devices = await self._get("device")
        except (aiohttp.ClientError, TimeoutError) as err:
            self.available = False
            _LOGGER.warning("Kan de Hue-bridge niet bevragen: %s", err)
            return

        self._scenes = {s["id"]: s.get("actions", []) for s in scenes}

        # Een room of zone verwijst via "children" naar DEVICES, niet naar
        # lampen. Home Assistant gebruikt de rid van de light-SERVICE als
        # unique_id, dus er is een tussenstap nodig: device -> light-service.
        # Zonder die stap levert de opzoeking niets op, want een device-rid
        # komt in het entiteitenregister niet voor.
        device_to_light: dict[str, str] = {}
        for dev in devices:
            for svc in dev.get("services", []):
                if svc.get("rtype") == "light":
                    device_to_light[dev["id"]] = svc["rid"]
                    break

        # De grouped_light-service van een room of zone is wat Home Assistant
        # als groepsentiteit toont; die rid is dus de sleutel.
        self._groups = {}
        for grp in rooms + zones:
            lampen = [
                device_to_light[c["rid"]]
                for c in grp.get("children", [])
                if c["rid"] in device_to_light
            ]
            for svc in grp.get("services", []):
                if svc.get("rtype") == "grouped_light":
                    self._groups[svc["rid"]] = lampen

        self._build_entity_map()
        self.available = True
        _LOGGER.debug(
            "Hue-definities ververst: %d scenes, %d groepen, %d entiteiten gekoppeld",
            len(self._scenes),
            len(self._groups),
            len(self._rid_to_entity),
        )

    async def _get(self, resource: str) -> list[dict[str, Any]]:
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{self._host}/clip/v2/resource/{resource}"
        async with session.get(
            url, headers={"hue-application-key": self._key}, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        return payload.get("data", [])

    def _build_entity_map(self) -> None:
        """Koppel Hue-resource-ids aan Home Assistant-entiteiten."""
        registry = er.async_get(self.hass)
        self._rid_to_entity = {
            entry.unique_id: entry.entity_id
            for entry in registry.entities.values()
            if entry.platform == HUE_DOMAIN and entry.unique_id
        }

    def entity_for(self, rid: str) -> str | None:
        return self._rid_to_entity.get(rid)

    def scene_targets(self, scene_entity: str) -> dict[str, dict[str, Any]]:
        """Geef per lamp-entiteit de gewenste eindstand van een scene."""
        registry = er.async_get(self.hass)
        entry = registry.async_get(scene_entity)
        if entry is None or entry.unique_id not in self._scenes:
            return {}

        doelen: dict[str, dict[str, Any]] = {}
        for actie in self._scenes[entry.unique_id]:
            lamp = self.entity_for(actie.get("target", {}).get("rid", ""))
            if lamp:
                doelen[lamp] = actie.get("action", {})
        return doelen

    def group_members(self, group_entity: str) -> list[str]:
        """Geef de lamp-entiteiten achter een Hue room- of zone-entiteit.

        Valt terug op het entity_id-attribuut dat de Hue-integratie zelf op de
        groepsentiteit zet, zodat dit ook werkt als de bridge onbereikbaar is.
        """
        registry = er.async_get(self.hass)
        entry = registry.async_get(group_entity)
        if entry and entry.unique_id in self._groups:
            leden = [self.entity_for(rid) for rid in self._groups[entry.unique_id]]
            gevonden = [x for x in leden if x]
            if gevonden:
                return gevonden

        state = self.hass.states.get(group_entity)
        if state:
            return list(state.attributes.get("entity_id") or [])
        return []
