# TP-Link Kasa PIR / Motion Sensor Patch for Home Assistant

A small custom integration that surfaces the **PIR motion sensor data** built
into TP-Link Kasa motion-sensor dimmer switches (ES20M, KS200M) as proper
Home Assistant entities — including a real `binary_sensor.<name>_motion` with
`device_class: motion` that you can use as an automation trigger.

The Kasa motion switches have always had this data available, and
[python-kasa](https://github.com/python-kasa/python-kasa) has exposed it since
[v0.10.0](https://github.com/python-kasa/python-kasa/pull/1263) (Jan 2025), but
the built-in Home Assistant `tplink` integration uses a whitelist of feature IDs
and never landed entity descriptions for the new PIR features. This shim adds
them — without forking the integration.

## What you get

For each supported Kasa motion-sensor switch:

| Entity | What it is |
|--------|-----------|
| `binary_sensor.<name>_motion` | Motion detected (device_class=motion). Use this in automation triggers. |
| `number.<name>_motion_sensor_threshold` | 0–100. Lower = more sensitive. |
| `select.<name>_motion_sensor_range` | Far / Mid / Near preset (default Far). |
| `number.<name>_motion_inactivity_timeout` | Hardware cold_time in ms (default 60000). Affects the switch's built-in auto-on behavior, not the binary_sensor. |
| `sensor.<name>_pir_value` | Signed deviation of the ADC reading from midpoint (the value compared to the threshold). |
| `sensor.<name>_pir_percentile` | Same, expressed as `%` of half-range. |
| `sensor.<name>_pir_adc_value` / `_min` / `_mid` / `_max` | Raw ADC (disabled-by-default diagnostic entities). |

The existing `switch.<name>_motion_sensor` (toggles the PIR on/off) is left
alone — it was already wired up upstream.

## Supported devices

Tested on **ES20M(US)** with HW 1.0 / firmware `1.1.6 Build 250522 Rel.210254`.

Should also work on:

- **KS200M** — same iot-protocol motion switch class as ES20M
- Any other device whose python-kasa Motion module (`kasa.iot.modules.motion.Motion`) initializes — the patch is generic over that module

Does **not** apply to smart-protocol Tapo motion sensors (P100M, etc.) — those
use `kasa.smart.modules.motionsensor` which already registers a proper
`motion_detected` binary_sensor upstream.

## Requirements

- Home Assistant **2025.2** or newer (ships python-kasa ≥ 0.10.0)
- HACS for the recommended install path
- The device must have polling enabled in HA — see [Polling](#polling) below

## Installation

### Via HACS (custom repository)

1. HACS → ⋮ (top-right) → **Custom repositories**
2. Repository: `https://github.com/CrazyCoder/hass-tplink-pir-patch`
3. Category: **Integration** → **Add**
4. Find "TP-Link Kasa PIR/Motion Sensor Patch" in the list → **Download**
5. Add one line to your `configuration.yaml`:

   ```yaml
   tplink_pir_patch:
   ```

6. Restart Home Assistant

### Manual install

Copy `custom_components/tplink_pir_patch/` to your HA `config/custom_components/`
directory, add `tplink_pir_patch:` to `configuration.yaml`, restart.

## Polling

HA's built-in tplink integration sometimes disables polling on devices that
look "manually-controlled-only" (e.g. light switches you only flip from HA).
The PIR data only updates while polling is active, so motion will never
appear to fire if your switch has polling off.

**Check:** Settings → Devices & Services → TP-Link → click the device → look
for a "Polling" toggle in the ⋮ menu. Enable it. Default poll interval is
5 seconds, which is also the motion-detection latency floor (local Kasa has
no push).

## Automation example

```yaml
- alias: "Master Vanity light on motion"
  trigger:
    - platform: state
      entity_id: binary_sensor.master_vanity_light_motion
      to: "on"
  action:
    - service: light.turn_on
      target:
        entity_id: light.master_vanity_light
```

Note that `binary_sensor.<name>_motion` flips between `on` and `off` based on
the **live PIR signal at every poll**, not on the switch's internal hardware
"motion-active window". So you handle `keep light on for N seconds after last
motion` in HA, not on the device. The hardware cold_time only matters if
you're also using the switch's built-in load-on-motion behavior managed by the
Kasa app's Smart Control rule.

## Sensitivity tuning

Trigger fires when `abs(pir_percentile) > (100 − threshold)`. So:

- Threshold **80** (default): triggers above 20% deviation. Conservative.
- Threshold **50**: needs 50% deviation. Less sensitive.
- Threshold **95**: triggers above 5%. Very sensitive — false-trigger prone.

Setting threshold manually flips the device to `Custom` range. The select
preset writes back the preset's threshold (Far=80, Mid=50, Near=20).

To see what your live PIR signal looks like, enable the diagnostic
`sensor.<name>_pir_percentile` entity and watch it while you walk in front
of the switch.

## Caveats

- **The Kasa app's "Smart Control" rule may revert hardware settings.**
  python-kasa's source warns that setting `pir_cold_time` may be reverted
  back to 60 seconds after a period of time unless the default Smart
  Control rule in the Kasa mobile app is deleted. Likely also applies to
  threshold and range changes. Not confirmed in this integration's testing —
  flagged here so it's quick to diagnose if HA-side settings drift on their
  own.

- **`binary_sensor.<name>_motion` is computed, not the device's hardware
  state.** python-kasa evaluates
  `enabled AND abs(pir_percent) > (100 − threshold)` on every poll using the
  raw ADC reading. So the binary_sensor tracks the live PIR signal at the
  poll interval rather than the switch's internal motion-active window.

- **5 s polling cadence is the latency floor** for local Kasa. Cannot be
  reduced without changing python-kasa's polling logic. Adequate for typical
  "turn light on when someone enters" patterns; too slow for fast-action
  automations.

## How it works (technical)

The shim is a tiny custom integration loaded via `configuration.yaml` so its
`async_setup` runs in HA's bootstrap stage 4 — before tplink config entries
are processed in stage 5.

It then applies four runtime patches:

1. **`kasa.iot.modules.motion.Motion._initialize_features`** is wrapped to:
   - flip `pir_triggered.type` from `Feature.Type.Sensor` to
     `Feature.Type.BinarySensor` (python-kasa registers it as Sensor returning
     a bool, which produces an awkward `"True"`/`"False"` sensor entity);
   - add a `pir_cold_time` Feature wrapping the existing
     `Motion.inactivity_timeout` property and `set_inactivity_timeout` setter
     (python-kasa exposes these but never registered them as a Feature);
   - wrap `pir_range`'s `attribute_getter` to return `range.name` instead of
     the `Range` enum object, so HA's `SelectEntity` (which compares
     `feature.value` directly to its options) doesn't show "Unknown".

2. **HA tplink description maps** (`SENSOR_DESCRIPTIONS_MAP`,
   `BINARYSENSOR_DESCRIPTIONS_MAP`, `NUMBER_DESCRIPTIONS_MAP`,
   `SELECT_DESCRIPTIONS_MAP`) get entries for each new feature, each with an
   explicit `name=` since there's no `strings.json` translation for these keys.

3. **`tplink.entity.FEATURES_ALLOW_LIST`** has `pir_triggered` added.
   `Feature.Category.Primary` features are filtered out for Dimmer device-types
   (where the light entity is the primary) unless explicitly allowlisted.

4. **`CoordinatedTPLinkFeatureEntity._description_for_feature`** is wrapped to
   preserve the input description's `name` field. HA's original calls
   `dataclasses.replace(desc, name=UNDEFINED)` on every description, clobbering
   any explicit name. Without this fix, our entities would render as
   `Master Vanity Light` with no suffix (only the device name) because there's
   no translation entry for the keys. Built-in tplink features that rely on
   translation are unaffected since their descriptions don't set `name`.

The description maps and `FEATURES_ALLOW_LIST` are plain dicts/sets at module
level, so direct mutation persists for the life of the HA process. python-kasa's
`Feature` is a non-frozen `@dataclass`, so post-init mutation of `.type` works.

## Upstream status

As of 2026-05, there's no related PR or issue in `home-assistant/core` and no
similar HACS integration on GitHub. The python-kasa PR #1263 author noted in
their PR description that the ADC value reporting was intended "so that it may
be used in polling automations" — they envisioned HA use — but never carried it
across the integration boundary themselves.

The right long-term fix is a PR to
[home-assistant/core](https://github.com/home-assistant/core) adding the entity
descriptions + `strings.json` entries directly to
`homeassistant/components/tplink/`, plus a python-kasa PR fixing the
`pir_range` Choice type mismatch. Until that lands, this shim works locally.

## Credit

- python-kasa PIR ADC PR
  [#1263](https://github.com/python-kasa/python-kasa/pull/1263) by
  [@ryenitcher](https://github.com/ryenitcher), reviewed and merged by
  [@sdb9696](https://github.com/sdb9696) — without this, none of the
  underlying data would be reachable from python-kasa.

## License

MIT. See [LICENSE](LICENSE).
