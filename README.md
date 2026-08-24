# Hue Insist

Makes sure a light command is actually carried out.

## The problem

A Hue group or scene is sent over Zigbee as a **groupcast**. Groupcast is not
acknowledged per lamp and is therefore never retried: a bulb with marginal range
misses the message for good. Individual light commands are sent as unicast, which
*is* acknowledged and which the Zigbee stack retries on its own.

Home Assistant never notices, because a group already counts as "on" the moment a
single member lights up. The result is a room that comes on halfway, with no error
and nothing to correct it.

Measured in a household with 34 Hue lights: one LED strip sat dark for tens of
minutes several times an evening while its group happily reported `on`. In one
case for 55 minutes straight.

## What this integration does

1. **Catches** every light request that passes through Home Assistant —
   automations, dashboards, HomeKit, Siri, voice assistants.
2. **Translates** it into a concrete target state per lamp. For scenes that comes
   straight from the definition on the Hue bridge, so brightness and colour are
   included, not just on/off.
3. **Verifies** after a configurable pause what actually happened.
4. **Corrects** each deviating lamp *individually*. That is the whole point: a
   single-target command does get acknowledged.
5. **Reports** what could not be fixed after all attempts, and keeps count of how
   often it had to step in.

Works without any configuration. Every setting has a defensible default.

## Installation

### Through HACS

Add this repository as a custom repository (category: Integration), install Hue
Insist, restart Home Assistant, then add the integration under
**Settings → Devices & Services → Add Integration**.

### Manually

Copy `custom_components/hue_insist` into the `custom_components` folder of your
Home Assistant configuration and restart.

## Options

| Option | Default | Notes |
|---|---|---|
| Number of attempts | 3 | How often a deviating lamp is retried |
| Delay | 2 s | Pause before verifying, and between attempts |
| Watch individual lights | on | Requests aimed at a single lamp |
| Watch groups and rooms | on | Requests aimed at a Hue room or zone |
| Watch scenes | on | Requests aimed at a scene |
| Verify brightness | on | Not just on/off, but the dim level too |
| Verify colour | on | Colour temperature and xy colour |
| Excluded lights | empty | Lamps you want left alone |
| Skip unavailable lamps | on | Lamps reporting `unavailable` are not verified |
| Correct anyway | empty | Exceptions to the line above |
| Brightness tolerance | 8 | On a 0-255 scale, a little over 3% |
| Colour temperature tolerance | 15 mired | Smaller differences are not visible |

## Entities

| Entity | Meaning |
|---|---|
| `sensor.hue_insist_corrections` | How often a lamp had to be nudged |
| `sensor.hue_insist_failures` | How often that did not work after all attempts |
| `sensor.hue_insist_last_failure` | Which lamp last refused to respond |

That first sensor is more than a counter: it shows which lamp has structurally
poor range. That is information you otherwise simply do not have.

## Events

| Event | Fired when |
|---|---|
| `hue_insist_corrected` | A lamp was nudged |
| `hue_insist_failed` | A lamp did not respond after all attempts |

Both carry `entities` and `source`, so you can hang a notification off them.

## How it reaches the Hue bridge

The integration reuses the credentials of the existing Hue integration in Home
Assistant. No second pairing, no pressing the button again.

The mapping between bridge and Home Assistant is exact: the Hue integration uses
the bridge resource id directly as the entity's `unique_id`, so a lookup in the
entity registry is enough. Groups go through `room.children` → device → light
service, because a room points at devices rather than at lights.

Without a Hue bridge the integration still works: group expansion falls back to
the `entity_id` attribute Home Assistant puts on group entities itself, and scenes
are not expanded.

## What it cannot see

**Control straight from the Hue app.** That never passes through Home Assistant
and is therefore invisible. Everything that does go through Home Assistant —
including HomeKit and Siri, as long as they run through the Home Assistant
bridge — is covered.

**Unavailable lamps**, by default. A lamp reporting `unavailable` is skipped and
does not count as a failure -- a bulb behind a door switch would otherwise be
retried every round and fail every time.

That default can be turned off globally, and there is a per-lamp exception list
for the cases where it gets in the way. The case this was built for: a Hue lamp
that no longer physically exists but is kept around as a proxy, its state read to
drive non-Hue hardware. Such a lamp reports `unavailable` while the command still
has to reach it. Lamps on the exception list are corrected blindly and are left
out of the failure tally, because there is nothing to verify against.

## Licence

MIT
