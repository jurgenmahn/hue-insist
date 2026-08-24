"""Config flow for Hue Insist.

The integration has to work without configuration: switching it on is enough.
Everything adjustable here has a defensible default, and the options exist for
tuning afterwards -- not as a hurdle up front.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

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


def _schema(current: dict[str, Any]) -> vol.Schema:
    def h(key, default):
        return current.get(key, default)

    return vol.Schema(
        {
            vol.Required(CONF_RETRIES, default=h(CONF_RETRIES, DEFAULT_RETRIES)):
                selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=10, step=1,
                                                  mode=selector.NumberSelectorMode.SLIDER)),
            vol.Required(CONF_DELAY, default=h(CONF_DELAY, DEFAULT_DELAY)):
                selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.5, max=15, step=0.5,
                                                  unit_of_measurement="s",
                                                  mode=selector.NumberSelectorMode.SLIDER)),
            vol.Required(CONF_WATCH_LIGHTS, default=h(CONF_WATCH_LIGHTS, True)):
                selector.BooleanSelector(),
            vol.Required(CONF_WATCH_GROUPS, default=h(CONF_WATCH_GROUPS, True)):
                selector.BooleanSelector(),
            vol.Required(CONF_WATCH_SCENES, default=h(CONF_WATCH_SCENES, True)):
                selector.BooleanSelector(),
            vol.Required(CONF_CHECK_BRIGHTNESS, default=h(CONF_CHECK_BRIGHTNESS, True)):
                selector.BooleanSelector(),
            vol.Required(CONF_CHECK_COLOR, default=h(CONF_CHECK_COLOR, True)):
                selector.BooleanSelector(),
            vol.Required(CONF_SKIP_UNAVAILABLE,
                         default=h(CONF_SKIP_UNAVAILABLE, DEFAULT_SKIP_UNAVAILABLE)):
                selector.BooleanSelector(),
            vol.Optional(CONF_UNAVAILABLE_EXCEPTIONS,
                         default=h(CONF_UNAVAILABLE_EXCEPTIONS, [])):
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="light", multiple=True)),
            vol.Optional(CONF_EXCLUDED, default=h(CONF_EXCLUDED, [])):
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="light", multiple=True)),
            vol.Required(CONF_BRIGHTNESS_TOLERANCE,
                         default=h(CONF_BRIGHTNESS_TOLERANCE, DEFAULT_BRIGHTNESS_TOLERANCE)):
                selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=64, step=1,
                                                  mode=selector.NumberSelectorMode.SLIDER)),
            vol.Required(CONF_SETTLE_TIMEOUT,
                         default=h(CONF_SETTLE_TIMEOUT, DEFAULT_SETTLE_TIMEOUT)):
                selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=60, step=1,
                                                  unit_of_measurement="s",
                                                  mode=selector.NumberSelectorMode.SLIDER)),
            vol.Required(CONF_COMMAND_RATE,
                         default=h(CONF_COMMAND_RATE, DEFAULT_COMMAND_RATE)):
                selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=50, step=1,
                                                  unit_of_measurement="/s",
                                                  mode=selector.NumberSelectorMode.SLIDER)),
            vol.Required(CONF_DEBUG_LOG, default=h(CONF_DEBUG_LOG, DEFAULT_DEBUG_LOG)):
                selector.BooleanSelector(),
            vol.Required(CONF_MIREK_TOLERANCE,
                         default=h(CONF_MIREK_TOLERANCE, DEFAULT_MIREK_TOLERANCE)):
                selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=100, step=1,
                                                  mode=selector.NumberSelectorMode.SLIDER)),
        }
    )


class HueInsistConfigFlow(ConfigFlow, domain=DOMAIN):
    """One-off setup. A second instance serves no purpose."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Hue Insist", data={}, options=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return HueInsistOptionsFlow()


class HueInsistOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init", data_schema=_schema(dict(self.config_entry.options))
        )
