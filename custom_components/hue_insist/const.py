"""Constanten voor Hue Insist."""

from __future__ import annotations

DOMAIN = "hue_insist"

# Configuratie-opties met hun standaardwaarden. De integratie moet zonder enige
# configuratie bruikbaar zijn, dus elke optie heeft een zinnige default.
CONF_RETRIES = "retries"
CONF_DELAY = "delay"
CONF_WATCH_LIGHTS = "watch_lights"
CONF_WATCH_SCENES = "watch_scenes"
CONF_WATCH_GROUPS = "watch_groups"
CONF_EXCLUDED = "excluded_entities"
CONF_CHECK_BRIGHTNESS = "check_brightness"
CONF_CHECK_COLOR = "check_color"
CONF_BRIGHTNESS_TOLERANCE = "brightness_tolerance"
CONF_MIREK_TOLERANCE = "mirek_tolerance"

DEFAULT_RETRIES = 3
DEFAULT_DELAY = 2.0
DEFAULT_BRIGHTNESS_TOLERANCE = 8   # op een schaal van 0-255, dus ruim 3%
DEFAULT_MIREK_TOLERANCE = 15       # mired; kleine afwijkingen zijn onzichtbaar

# Hue levert helderheid als percentage, Home Assistant als 0-255.
HUE_BRIGHTNESS_MAX = 100.0
HA_BRIGHTNESS_MAX = 255.0

SIGNAL_JOB_FINISHED = f"{DOMAIN}_job_finished"

EVENT_CORRECTED = f"{DOMAIN}_corrected"
EVENT_FAILED = f"{DOMAIN}_failed"
