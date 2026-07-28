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
| `button.<name>_safe_restart` | Reboots the switch and re-arms the PIR afterwards. See [Rebooting disarms the PIR](#rebooting-disarms-the-pir). |

The existing `switch.<name>_motion_sensor` (toggles the PIR on/off) is left
alone — it was already wired up upstream.

## Supported devices

Developed against **ES20M(US)**, HW 1.0, firmware `1.1.6 Build 250522 Rel.210254`.
These units lock up under polling, see [Lockups](#lockups-on-es20m--ks220m).

Should also work on:

- **KS200M** — same iot-protocol motion switch class as ES20M
- Any other device whose python-kasa Motion module (`kasa.iot.modules.motion.Motion`) initializes — the patch is generic over that module

Does **not** apply to smart-protocol Tapo motion sensors (P100M, etc.) — those
use `kasa.smart.modules.motionsensor` which already registers a proper
`motion_detected` binary_sensor upstream.

## Requirements

- Home Assistant **2025.2** or newer (ships python-kasa ≥ 0.10.0)
- HACS for the recommended install path
- The device must have polling enabled in HA — see [Polling](#polling) below.
  On ES20M and KS220M, read [Lockups](#lockups-on-es20m--ks220m) first.

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

### Lockups on ES20M / KS220M

A lot of ES20M and KS220M owners report the switch hard-locking after hours to
days of Home Assistant polling. The physical button stops responding and only
the reset button under the paddle brings it back. See
[home-assistant/core#150044](https://github.com/home-assistant/core/issues/150044)
and TP-Link threads
[840580](https://community.tp-link.com/en/smart-home/forum/topic/840580),
[849636](https://community.tp-link.com/en/home/forum/topic/849636),
[855150](https://community.tp-link.com/en/home/forum/topic/855150). It happens
to mine.

This is not caused by this integration, and installing it does not make it more
likely. python-kasa's `Motion.query()` merges `get_config` and `get_adc_value`
into every update for these devices regardless of which entities exist, so stock
HA has been reading the PIR ADC every 5 s since python-kasa 0.10.0. The patch
adds no requests of its own.

The same fact cuts the other way: disabling the PIR entities does not reduce
polling load, only turning polling off does.
[Confirmed by python-kasa's maintainer](https://github.com/home-assistant/core/issues/150044#issuecomment-3303540374).

So on an affected switch you get one or the other, since
`binary_sensor.<name>_motion` only updates while polling is on. I run mine with
polling off and no motion entity.

| Mitigation | Outcome |
|---|---|
| Disable polling for that device (⋮ → System options on its TP-Link device page) | Weeks of stability for several reporters, and for me. Motion entity stops updating. |
| Turn off Smart Control in the Kasa app, plus the motion and ambient light sensors | [Freezes stopped, and came back immediately on re-enabling](https://github.com/home-assistant/core/issues/150044#issuecomment-3863725049) |
| Firmware 1.1.6 Build 250522 Rel.210254 | TP-Link's fix for the 1.1.5 freeze bug. [People still freeze on 1.1.6](https://community.tp-link.com/en/home/forum/topic/855150), including me. Update anyway. |
| Poll interval raised to 30 s | [Still froze](https://github.com/home-assistant/core/issues/150044#issuecomment-4694398074). Not a fix by itself. |

TP-Link's position is that Home Assistant is unsupported and you should
disconnect from it.

A rebooted switch comes back with its load state intact (on stays on, off stays
off), so a scheduled restart is cheap. The one thing it does break is the motion
sensor, which is why this integration ships a restart button that handles it.

### Rebooting disarms the PIR

**After any reboot the switch stops reacting to motion, while continuing to
report that the sensor is enabled.** `smartlife.iot.PIR get_config` still returns
`enable: 1`, HA still shows the Motion sensor switch as on, but the device's
built-in Smart Control no longer drives the load and `pir_triggered` stops
firing. Toggling the sensor off and on again restores it.

This applies to every reboot, including the physical reset button — which is the
one TP-Link tells you to press when the switch freezes. So the standard advice
for the lockup silently leaves your motion sensor dead.

`button.<name>_safe_restart` does the whole sequence: reboot, wait for the device
to answer again, then toggle `set_enabled` off and back on. It skips the re-arm
when the PIR was already disabled. The press blocks until the switch is back, up
to 120 s — a healthy ES20M answers again in about 8 s, a degraded one has been
measured at over 40 s. If the device never comes back it logs a warning telling
you to toggle the sensor by hand.

Home Assistant also has a stock `button.<name>_restart` for every Kasa device,
supplied by python-kasa's `reboot` feature and hidden because that feature is
`Category.Debug`. Enable it under the device's entity list if you want it. It
does **not** re-arm the PIR.

A nightly restart is worth trying as a mitigation for the lockups above, on the
theory that the fault accumulates and a reboot is the only thing that resets it:

```yaml
- alias: "Kasa ES20M: nightly preventive reboot"
  triggers:
    - trigger: time
      at: "04:00:00"
  actions:
    - action: button.press
      target:
        entity_id: button.garage_light_safe_restart
      continue_on_error: true
```

Unverified as a fix — it is being tested. Reported here because nobody in
[core#150044](https://github.com/home-assistant/core/issues/150044) has tried it.

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

The shim is a tiny custom integration loaded via `configuration.yaml`. It and
tplink are set up concurrently in the same bootstrap stage, so ordering is
what matters: `async_setup` is deliberately **await-free**, which makes the
description-map mutations below atomic with respect to the event loop and
guarantees they land before tplink forwards its platform setups.

For the same reason, python-kasa and the tplink platform modules are imported
at **module scope** rather than inside `async_setup`. HA imports
custom-integration modules in its dedicated import executor, so this keeps
Python's import machinery (which scans `sys.path`, including `/config/deps`,
and reads dist-info metadata) off the event loop — importing inside
`async_setup` tripped HA's blocking-call detector and stalled the loop for
~1.4 s ([#1](https://github.com/CrazyCoder/hass-tplink-pir-patch/issues/1)).

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
