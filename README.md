# Hue Insist

Makes sure a light command is actually carried out.

## The problem

You switch on the kitchen and one lamp stays dark. You do it again and it works
fine. Home Assistant reports nothing wrong: the automation ran, the scene was
applied, the group is on.

The cause sits one layer down, in Zigbee. A group or scene goes out as a
**groupcast** -- a single message to every lamp at once, which no lamp
acknowledges and which is therefore never resent. A bulb that is awkwardly
placed or briefly busy misses it, and no error is raised anywhere. A command
aimed at one lamp goes out as **unicast** instead: acknowledged, and resent by
the Zigbee stack itself when that acknowledgement fails to arrive.

Home Assistant cannot tell the two apart, because a group counts as "on" the
moment one member lights up. So the room comes on halfway and stays that way
until somebody notices and presses again.

In the house this was built for -- 34 Hue lights -- one LED strip did exactly
that several times an evening, sitting dark for tens of minutes while its group
reported on. Once for 55 minutes straight.

That is the gap this integration closes: it re-sends the missed command as
unicast, so the lamp gets a message it has to acknowledge.

## What it does

1. **Catches** every light request that passes through Home Assistant --
   automations, dashboards, HomeKit, Siri, voice assistants.
2. **Translates** it into a concrete target state per lamp. For scenes that comes
   straight from the definition on the Hue bridge, so brightness and colour are
   included, not just on/off.
3. **Verifies** what actually happened, once the bridge has stopped working
   through the request.
4. **Corrects** each deviating lamp *individually*, paced to what the bridge can
   take. Individual is the whole point: a single-target command does get
   acknowledged.
5. **Reports** what could not be fixed, and keeps count of how often it stepped
   in and for which lamp.

Works without any configuration. Every setting has a defensible default.

## Installation

**Through HACS.** Add this repository as a custom repository (category:
Integration), install Hue Insist, restart Home Assistant, then add the
integration under **Settings → Devices & Services → Add Integration**.

**Manually.** Copy `custom_components/hue_insist` into the `custom_components`
folder of your Home Assistant configuration and restart.

## Options

Everything is adjustable from the integration's options, and changes take effect
immediately -- no restart.

**What to watch**

| Option | Default | Notes |
|---|---|---|
| Watch individual lights | on | Requests aimed at a single lamp |
| Watch groups and rooms | on | Requests aimed at a Hue room or zone |
| Watch scenes | on | Requests aimed at a scene |
| Lights to skip | empty | Lamps to leave alone entirely |

**What counts as wrong**

| Option | Default | Notes |
|---|---|---|
| Verify brightness as well | on | Not just on/off, but the dim level too |
| Verify colour as well | on | Colour temperature and xy colour |
| Brightness tolerance (0-255) | 8 | A little over 3% |
| Colour temperature tolerance (mired) | 15 | Smaller differences are not visible |
| Skip unavailable lamps | on | Lamps reporting `unavailable` are not verified |
| Correct anyway, even when they report unavailable | empty | Per-lamp exceptions to the line above |

**Timing**

| Option | Default | Notes |
|---|---|---|
| Number of attempts | 3 | How often a deviating lamp is retried |
| Minimum wait before verifying | 2 s | Floor under every check, and the pause between attempts |
| Maximum extra wait for the bridge to finish | 15 s | Ceiling on waiting for a large request; 0 disables |
| Maximum commands per second | 10 | How fast corrections are sent to the bridge |

**Diagnostics**

| Option | Default | Notes |
|---|---|---|
| Verbose logging | off | A line per request, per deviating lamp and per correction |

### The two waits

A fixed verification delay is right for one lamp and wrong for a whole house.
Switching thirty lamps takes the bridge several seconds, so checking the result
after two means "correcting" lamps whose turn had simply not come yet -- and
those corrections pile onto the queue that is already the bottleneck. The
integration ends up racing the bridge and losing. Hence two settings rather than
one.

**Minimum wait** is the floor: the grace period the bridge gets to start. Right
after a command no lamp has reported back yet, so a check at that moment would
judge the bridge before it began and every lamp would look like it had missed
the message.

**Maximum extra wait** is the ceiling on what comes after. Once the minimum has
passed, verification holds off for as long as the lamps are still changing state:
rather than guessing how long the bridge needs, watch it work. Every lamp it
reaches updates its state in Home Assistant, so once those updates stop for a
second the request has been carried out as far as it is going to be.

The two never overlap. A single lamp is verified after the minimum wait and no
longer, because a bridge that has already finished produces no further state
changes -- so nothing got slower. A request touching thirty lamps is verified
once the bridge actually stops, however long that takes, up to the ceiling. The
ceiling exists for the lamp that never stops changing; setting it to 0 leaves
only the fixed minimum.

Measured: a request the bridge spends four seconds on is verified after about
five.

### Why the commands are paced

The Hue bridge handles roughly ten light commands per second and silently drops
whatever arrives on top of that -- no error, no retry, nothing in the log. Firing
thirty corrections at once therefore repairs almost nothing and adds to the
congestion that caused the problem.

Measured: thirty lamps take about three seconds to correct at the default rate.
That is longer than the minimum wait, which is fine -- the next check simply
shifts along. Raise the rate if your bridge keeps up; lower it if corrections
still go missing.

### Verbose logging

The switch sets the log level for this integration on the fly, so there is no
need to edit `configuration.yaml` or restart. Turning it off restores whatever
level was configured before, so a deliberate `logger:` setting is left intact.

What it writes, per request:

```
Caught light.turn_on on light.kitchen [group] -> 6 lamp(s), target on bri 254 2700K
light.kitchen expands to 6 lamp(s): light.spot_1, light.spot_2, ...
light.turn_on attempt 1/3: 2 of 6 lamp(s) deviate
  light.spot_3: off, expected on
  light.spot_4: brightness 140, expected 254
  -> light.turn_on light.spot_3 (on bri 254 2700K)
  -> light.turn_on light.spot_4 (on bri 254 2700K)
light.turn_on: all 6 lamp(s) correct after attempt 2
```

The reason per lamp is the useful part. "off, expected on" is a missed command;
"brightness 140, expected 254" is a lamp that got the message but landed
somewhere else. Only the first is the problem this integration was built for.

## Services

Three services ride along with the watcher, because they need the same two
things it already knows: which lamps hide behind a group, and how to talk to the
bridge without drowning it.

### `hue_insist.flash_lights`

Pulses lights on and off for as long as you ask. Unlike `light.turn_on` with
`flash: short`, which blinks everything once and simultaneously, this repeats --
so the flash is hard to miss -- and can walk through a random subset of lamps
per pulse.

| Field | Default | Meaning |
|---|---|---|
| `flash_duration` | 3000 | total time to keep pulsing, in ms |
| `on_duration` | 250 | how long a lamp stays on per pulse, in ms |
| `off_duration` | 250 | how long it stays off between pulses, in ms |
| `concurrent_lights` | 0 | lamps per pulse; 0 means all of them |
| `brightness` | -- | brightness of the on-pulse |

Mind the bridge budget. A pulse over N lamps costs N commands and every pulse
has an on and an off, so `concurrent_lights: 6` at 200ms on and 200ms off asks
for 30 commands per second against a budget of about ten. The service logs a
warning when the arithmetic does not fit; the blinks that do not fit are dropped
silently by the bridge, as always.

Targeting a single Hue room or zone with `concurrent_lights: 0` is the exception:
that goes out as one groupcast per pulse, so only two commands per cycle
regardless of how many lamps hang behind it.

Nothing here is verified or retried. A missed blink stays missed, because
insisting on a flash would leave the lamp switched on -- the opposite of what a
flash is for. For the same reason the watcher ignores any `light.turn_on` that
carries `flash`.

### `hue_insist.save_state` and `hue_insist.restore_state`

Named snapshots of light state, held in memory. The motivating case is a
doorbell flash: something has to put the lights back afterwards.

```yaml
- action: hue_insist.save_state
  data: {name: doorbell}
- action: hue_insist.flash_lights
  target: {entity_id: light.home}
  data: {flash_duration: 5000, on_duration: 50, off_duration: 25}
- action: hue_insist.restore_state
  data: {name: doorbell}
```

`save_state` takes an optional `entity_id`; leave it out to capture every light
in the house. Groups are expanded to their members, because restoring a room
entity would push one aggregate state onto every lamp behind it and wipe the
per-lamp detail the snapshot exists to preserve.

`restore_state` drops the snapshot once used unless you pass `clear: false`.
Lamps that need the same end state are batched into one call, so putting 33
lamps back typically costs eight commands rather than 33. The restore itself
goes out through `light.turn_on` / `light.turn_off`, so the watcher verifies it
like any other command.

Snapshots do not survive a restart. They exist to bridge a few seconds; a stale
one restored hours later would be worse than none at all.

## Sensors

All sensors are diagnostic and survive a restart -- counters and timestamps
alike -- so a problem that happens a few times a day still adds up to a number.

**Is it running?**

| Entity | Meaning |
|---|---|
| `sensor.hue_insist_captured_actions` | Light requests caught |
| `sensor.hue_insist_captured_scene_actions` | ... of which aimed at a scene |
| `sensor.hue_insist_captured_group_actions` | ... at a room or zone |
| `sensor.hue_insist_captured_device_actions` | ... at a single lamp |
| `sensor.hue_insist_last_action` | When the last request came in |

A request that targets a group and a loose lamp at once is counted in both
categories, so the three splits can add up to more than the total.

**Is anything wrong?**

| Entity | Meaning |
|---|---|
| `sensor.hue_insist_checked_devices` | Lamp states verified, after expanding scenes and groups |
| `sensor.hue_insist_no_correction_needed` | ... of which were already correct |
| `sensor.hue_insist_corrections` | How many lamps had to be nudged |
| `sensor.hue_insist_failures` | How many did not respond after all attempts |
| `sensor.hue_insist_last_correction` | When a lamp was last nudged |
| `sensor.hue_insist_last_failure` | When a lamp last refused; the `entities` attribute names it |

The gap between checked devices and corrections is the interesting number. It
turns "I think that lamp misses commands sometimes" into evidence, and it names
the lamp.

Both checks and corrections are counted once per request rather than once per
retry round. A lamp that needs three attempts is still one lamp that needed
nudging; counting per round would multiply it by the retry count and push
corrections above the number of lamps checked, which is impossible on its face.

## Events

| Event | Fired when |
|---|---|
| `hue_insist_corrected` | A lamp was nudged |
| `hue_insist_failed` | A lamp did not respond after all attempts |

Both carry `entities` and `source`, so you can hang a notification off them.

## Finding a bad lamp

Turn on verbose logging and leave it for a day. Then read
`sensor.hue_insist_corrections` against `sensor.hue_insist_checked_devices`, and
grep the log for the lamps that keep appearing. A lamp that shows up as "off,
expected on" several times an evening has a range problem; one that never appears
is fine. That is the diagnosis this integration exists to produce -- the repair
is a side effect.

## How it works

### Not only Hue

The catching is integration-agnostic: every `light.turn_on`, `light.turn_off`
and `light.toggle` that passes through Home Assistant is verified, whatever
platform the entity belongs to. A Tuya, Zigbee2MQTT, WLED or Shelly light gets
the same treatment, because a command that silently fails to arrive is not a
uniquely Hue problem.

The Hue bridge is only needed to expand scenes and groups into their members and
to know what a scene means per lamp. Everything else -- the state check, the
per-lamp correction -- runs on Home Assistant's own view of the entity.

### Reaching the bridge

The integration reuses the credentials of the existing Hue integration. No second
pairing, no pressing the button again.

The mapping between bridge and Home Assistant is exact: the Hue integration uses
the bridge resource id directly as the entity's `unique_id`, so a lookup in the
entity registry is enough. Groups go through `room.children` → device → light
service, because a room points at devices rather than at lights.

Without a Hue bridge the integration still works: group expansion falls back to
the `entity_id` attribute Home Assistant puts on group entities itself, and
scenes are not expanded.

## What it deliberately does not judge

The rule throughout: never report a failure that can never be fixed. A lamp
condemned for something it cannot do would be corrected every round, fail every
time, and bury the real problems.

**Anything a lamp cannot do.** Verification is limited to the capabilities the
entity reports through `supported_color_modes`. A Hue smart plug reports
`["onoff"]`: it has no brightness, so a scene brightness is neither checked
against it nor sent to it.

**Colour across colour modes.** A light reports only the attribute belonging to
the mode it is currently in, so a lamp sitting in xy has no colour temperature to
compare against. Converting between the two loses far too much to judge on --
Home Assistant's own round trip from 2278K through xy returns 1718K, over 140
mired out against a tolerance of 15. Such a mismatch is logged, with what the
lamp actually reports, and left alone. Third-party Zigbee bulbs joined to a Hue
bridge are the usual cause: the bridge resolves the requested colour temperature
to xy for that lamp's gamut, and xy is what comes back.

**Unavailable lamps**, by default. A bulb behind a door switch would otherwise be
retried every round and fail every time. That default can be turned off globally,
and there is a per-lamp exception list for the cases where it gets in the way.

The case the exception list was built for: a Hue lamp that no longer physically
exists but is kept around as a proxy, its state read to drive non-Hue hardware.
Such a lamp reports `unavailable` while the command still has to reach it. Lamps
on the list are corrected blindly and left out of the failure tally, because
there is nothing to verify against.

## What it cannot see

**Control straight from the Hue app.** That never passes through Home Assistant
and is therefore invisible. Everything that does go through Home Assistant --
including HomeKit and Siri, as long as they run through the Home Assistant
bridge -- is covered.

## Licence

MIT
