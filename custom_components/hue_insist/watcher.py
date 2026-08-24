"""Vangt lichtcommando's op, controleert het resultaat en herstelt wat misging.

Het probleem dat dit oplost: een Hue-groep of scene wordt door de bridge als
Zigbee groupcast verstuurd. Groupcast kent geen bevestiging per lamp en dus ook
geen herhaling op Zigbee-niveau. Een lamp met matig bereik mist het bericht
definitief, en niemand merkt het -- Home Assistant toont de groep als aan zodra
een van de leden brandt.

De aanpak: elk verzoek dat via Home Assistant loopt wordt gevangen, vertaald naar
een concrete gewenste eindstand per lamp, en na een korte pauze geverifieerd. Wat
niet klopt wordt per lamp afzonderlijk gecorrigeerd -- unicast, dus mét
bevestiging en met herhaling door de Zigbee-stack zelf.

Wat hier bewust buiten valt: bediening rechtstreeks in de Hue-app. Die loopt niet
langs Home Assistant en is dus onzichtbaar.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_OFF
from homeassistant.core import Context, Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    EVENT_CORRECTED,
    EVENT_FAILED,
    HA_BRIGHTNESS_MAX,
    HUE_BRIGHTNESS_MAX,
)
from .hue_api import HueDefinitions

_LOGGER = logging.getLogger(__name__)

ONBEKEND = (None, "unknown", "unavailable")


@dataclass
class Doel:
    """De gewenste eindstand van een enkele lamp."""

    aan: bool
    brightness: int | None = None      # 0-255, zoals Home Assistant hem kent
    kelvin: int | None = None
    xy: tuple[float, float] | None = None

    @classmethod
    def van_hue_actie(cls, actie: dict[str, Any]) -> "Doel":
        """Vertaal een scene-actie van de bridge naar een doelstand."""
        aan = bool(actie.get("on", {}).get("on", True))
        helderheid = None
        if "dimming" in actie:
            pct = float(actie["dimming"].get("brightness", 0))
            helderheid = round(pct / HUE_BRIGHTNESS_MAX * HA_BRIGHTNESS_MAX)
        kelvin = None
        if "color_temperature" in actie:
            mirek = actie["color_temperature"].get("mirek")
            if mirek:
                kelvin = round(1_000_000 / int(mirek))
        xy = None
        if "color" in actie and "xy" in actie["color"]:
            punt = actie["color"]["xy"]
            xy = (float(punt["x"]), float(punt["y"]))
        return cls(aan=aan, brightness=helderheid, kelvin=kelvin, xy=xy)

    @classmethod
    def van_service_data(cls, service: str, data: dict[str, Any]) -> "Doel":
        """Vertaal een light.turn_on/turn_off aanroep naar een doelstand."""
        if service == "turn_off":
            return cls(aan=False)

        helderheid = data.get("brightness")
        if helderheid is None and "brightness_pct" in data:
            helderheid = round(float(data["brightness_pct"]) / 100 * HA_BRIGHTNESS_MAX)
        kelvin = data.get("color_temp_kelvin")
        if kelvin is None and data.get("color_temp"):
            kelvin = round(1_000_000 / int(data["color_temp"]))
        xy = tuple(data["xy_color"]) if data.get("xy_color") else None
        return cls(aan=True, brightness=helderheid, kelvin=kelvin, xy=xy)

    def naar_service_data(self) -> tuple[str, dict[str, Any]]:
        """Wat er nodig is om deze stand af te dwingen op een enkele lamp."""
        if not self.aan:
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
class Opdracht:
    """Een gevangen verzoek, wachtend op verificatie."""

    doelen: dict[str, Doel]
    poging: int = 0
    herkomst: str = ""
    mislukt: list[str] = field(default_factory=list)


class Watcher:
    """Luistert, verifieert en corrigeert."""

    def __init__(self, hass: HomeAssistant, definities: HueDefinitions, opties) -> None:
        self.hass = hass
        self.definities = definities
        self.opties = opties
        self._eigen_contexten: set[str] = set()
        self._lopend: dict[str, asyncio.Task] = {}
        self.correcties = 0
        self.mislukkingen = 0
        self.laatste_fout: str | None = None

    @callback
    def behandel_event(self, event: Event) -> None:
        """Beoordeel een call_service-event en zet zo nodig een controle uit."""
        data = event.data
        domein = data.get("domain")
        dienst = data.get("service")

        # Onze eigen correcties mogen geen nieuwe controle uitlokken; dat zou een
        # oneindige lus opleveren. De context waarmee wij aanroepen is bekend.
        if event.context and event.context.id in self._eigen_contexten:
            return

        if domein == "light" and dienst in ("turn_on", "turn_off", "toggle"):
            doelen = self._doelen_voor_licht(dienst, data.get("service_data", {}))
            herkomst = f"light.{dienst}"
        elif domein == "scene" and dienst == "turn_on" and self.opties.watch_scenes:
            doelen = self._doelen_voor_scene(data.get("service_data", {}))
            herkomst = "scene.turn_on"
        else:
            return

        doelen = {
            e: d for e, d in doelen.items() if e not in self.opties.excluded
        }
        if doelen:
            self.hass.async_create_task(
                self._controleer(Opdracht(doelen=doelen, herkomst=herkomst))
            )

    def _entiteiten_uit(self, service_data: dict[str, Any]) -> list[str]:
        ruw = service_data.get(ATTR_ENTITY_ID) or []
        return [ruw] if isinstance(ruw, str) else list(ruw)

    def _doelen_voor_licht(self, dienst: str, data: dict[str, Any]) -> dict[str, Doel]:
        doelen: dict[str, Doel] = {}
        for entiteit in self._entiteiten_uit(data):
            leden = self.definities.group_members(entiteit)
            is_groep = bool(leden)

            if is_groep and not self.opties.watch_groups:
                continue
            if not is_groep and not self.opties.watch_lights:
                continue

            if dienst == "toggle":
                # De bedoelde eindstand hangt af van de stand op dit moment, dus
                # die moet nu vastgelegd worden -- na de wachttijd is hij weg.
                huidig = self.hass.states.get(entiteit)
                aan_straks = not (huidig and huidig.state == STATE_ON)
                doel = Doel(aan=aan_straks)
            else:
                doel = Doel.van_service_data(dienst, data)

            for lamp in (leden or [entiteit]):
                doelen[lamp] = doel
        return doelen

    def _doelen_voor_scene(self, data: dict[str, Any]) -> dict[str, Doel]:
        doelen: dict[str, Doel] = {}
        for scene in self._entiteiten_uit(data):
            for lamp, actie in self.definities.scene_targets(scene).items():
                doelen[lamp] = Doel.van_hue_actie(actie)
        return doelen

    async def _controleer(self, opdracht: Opdracht) -> None:
        """Wacht, vergelijk, en corrigeer wat afwijkt."""
        for poging in range(1, self.opties.retries + 1):
            await asyncio.sleep(self.opties.delay)
            opdracht.poging = poging

            afwijkend = {
                lamp: doel
                for lamp, doel in opdracht.doelen.items()
                if self._wijkt_af(lamp, doel)
            }
            if not afwijkend:
                if poging > 1:
                    _LOGGER.debug("Alles goed na poging %d", poging)
                return

            _LOGGER.debug(
                "Poging %d: %d lamp(en) wijken af: %s",
                poging, len(afwijkend), ", ".join(afwijkend),
            )
            await self._corrigeer(afwijkend)
            self.correcties += len(afwijkend)
            self.hass.bus.async_fire(
                EVENT_CORRECTED,
                {"entities": list(afwijkend), "attempt": poging, "source": opdracht.herkomst},
            )

        # Na de laatste poging nog steeds mis: opgeven en melden.
        rest = [l for l, d in opdracht.doelen.items() if self._wijkt_af(l, d)]
        if rest:
            self.mislukkingen += len(rest)
            self.laatste_fout = ", ".join(rest)
            _LOGGER.warning(
                "Na %d pogingen niet gelukt: %s (aanleiding: %s)",
                self.opties.retries, self.laatste_fout, opdracht.herkomst,
            )
            self.hass.bus.async_fire(
                EVENT_FAILED,
                {"entities": rest, "attempts": self.opties.retries, "source": opdracht.herkomst},
            )

    def _wijkt_af(self, lamp: str, doel: Doel) -> bool:
        """Bepaal of een lamp niet is wat er gevraagd werd."""
        state = self.hass.states.get(lamp)
        if state is None or state.state in ONBEKEND:
            # Onbereikbaar valt buiten beschouwing. Een lamp zonder stroom --
            # bijvoorbeeld achter een deurschakelaar in een kast -- zou anders
            # elke ronde opnieuw geprobeerd worden en altijd als fout eindigen.
            return False

        werkelijk_aan = state.state == STATE_ON
        if werkelijk_aan != doel.aan:
            return True
        if not doel.aan:
            return False

        attrs = state.attributes
        if self.opties.check_brightness and doel.brightness is not None:
            nu = attrs.get("brightness")
            if nu is None or abs(int(nu) - doel.brightness) > self.opties.brightness_tolerance:
                return True

        if self.opties.check_color:
            if doel.xy is not None:
                nu = attrs.get("xy_color")
                if nu is None or max(abs(nu[0] - doel.xy[0]), abs(nu[1] - doel.xy[1])) > 0.02:
                    return True
            elif doel.kelvin is not None:
                nu = attrs.get("color_temp_kelvin")
                if nu is None:
                    return True
                # Vergelijken in mired: gelijke stappen zijn daar visueel gelijk,
                # in kelvin niet. 100K bij 2000K is zichtbaar, bij 6000K niet.
                verschil = abs(1_000_000 / int(nu) - 1_000_000 / doel.kelvin)
                if verschil > self.opties.mirek_tolerance:
                    return True
        return False

    async def _corrigeer(self, afwijkend: dict[str, Doel]) -> None:
        """Stuur elke lamp afzonderlijk aan.

        Afzonderlijk en niet als groep: een unicast-commando wordt door de
        Zigbee-stack bevestigd en zo nodig herhaald, een groupcast niet. Dat is
        precies het verschil waar dit hele mechanisme om draait.
        """
        for lamp, doel in afwijkend.items():
            dienst, data = doel.naar_service_data()
            context = Context()
            self._eigen_contexten.add(context.id)
            if len(self._eigen_contexten) > 500:
                self._eigen_contexten = set(list(self._eigen_contexten)[-250:])
            await self.hass.services.async_call(
                "light", dienst, {ATTR_ENTITY_ID: lamp, **data},
                blocking=False, context=context,
            )
