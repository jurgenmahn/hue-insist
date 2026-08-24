"""Constants for Hue Insist."""

from __future__ import annotations

DOMAIN = "hue_insist"

# Configuration options with their defaults. The integration has to be usable
# without any configuration at all, so every option has a sensible default.
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
CONF_SKIP_UNAVAILABLE = "skip_unavailable"
CONF_UNAVAILABLE_EXCEPTIONS = "unavailable_exceptions"
CONF_COMMAND_RATE = "command_rate"
CONF_DEBUG_LOG = "debug_log"
CONF_SETTLE_TIMEOUT = "settle_timeout"

DEFAULT_RETRIES = 3
DEFAULT_DELAY = 2.0
DEFAULT_BRIGHTNESS_TOLERANCE = 8   # on a 0-255 scale, a little over 3%
DEFAULT_MIREK_TOLERANCE = 15       # mired; smaller differences are invisible

# Skipping unavailable lamps is the right default: a bulb without power would
# otherwise be retried every round and fail every time. But some setups abuse a
# Hue lamp that no longer physically exists as a proxy -- its state is read to
# drive something else entirely. Those need the command even though they can
# never confirm receiving it, hence the per-entity exception list.
DEFAULT_SKIP_UNAVAILABLE = True

# The Hue bridge accepts roughly ten light commands per second and silently
# drops the rest -- no error, no retry, nothing in the log. Correcting a room of
# thirty lamps therefore has to be paced, or most of the corrections evaporate
# and the integration works against itself.
DEFAULT_COMMAND_RATE = 10

# Off by default: a line per caught request, per deviating lamp and per
# correction is exactly what you want while diagnosing a lamp, and far too much
# the rest of the time.
DEFAULT_DEBUG_LOG = False

# A fixed verification delay is right for one lamp and wrong for a whole house:
# switching thirty lamps takes the bridge several seconds, and judging the result
# after two means "correcting" lamps whose turn had not come yet. Rather than
# guessing how long the bridge needs, wait until the lamps stop changing state.
# This is the ceiling on that extra wait; 0 switches the behaviour off.
DEFAULT_SETTLE_TIMEOUT = 15

# Hue reports brightness as a percentage, Home Assistant as 0-255.
HUE_BRIGHTNESS_MAX = 100.0
HA_BRIGHTNESS_MAX = 255.0

# Internal refresh signal for the diagnostic sensors. Deliberately a dispatcher
# signal and not an event: the event bus is a public interface, and "a counter
# went up" is nobody else's business.
SIGNAL_STATS_UPDATED = f"{DOMAIN}_stats_updated"

EVENT_CORRECTED = f"{DOMAIN}_corrected"
EVENT_FAILED = f"{DOMAIN}_failed"
