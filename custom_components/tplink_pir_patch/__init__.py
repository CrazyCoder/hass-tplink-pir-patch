"""Surface python-kasa PIR features as proper HA entities.

For ES20M / KS200M motion-sensor switches (iot module). Adds:
  - binary_sensor pir_triggered (device_class=motion) — flipped from python-kasa's Sensor
  - sensor pir_value / pir_percent / pir_adc_* (debug ones disabled by default)
  - number pir_cold_time — inactivity timeout (python-kasa exposes the setter but no Feature)
  - button reboot_safe — reboot that re-arms the PIR afterwards (see _safe_reboot)

Loaded via configuration.yaml. tplink is set up concurrently in the same
bootstrap stage, so the SENSOR/NUMBER/BINARY_SENSOR description maps must be
mutated before tplink forwards its platform setups — see async_setup.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, NamedTuple

import dataclasses

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.button import ButtonDeviceClass
from homeassistant.components.number import NumberDeviceClass, NumberMode
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import UNDEFINED, ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "tplink_pir_patch"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

# Seconds to wait after issuing the reboot before the first liveness probe, how
# long to keep probing, and the off->on gap of the PIR re-arm. A healthy ES20M
# answers again after ~8 s; a degraded one has been measured at over 40 s.
_REBOOT_SETTLE = 8
_REBOOT_WAIT = 120
_PIR_TOGGLE_GAP = 1.0


class _Deps(NamedTuple):
    """The python-kasa / HA tplink modules the patches operate on."""

    Feature: Any
    Motion: Any
    tplink_bs: Any
    tplink_button: Any
    tplink_entity: Any
    tplink_number: Any
    tplink_select: Any
    tplink_sensor: Any


def _load_deps() -> _Deps:
    """Import python-kasa and the HA tplink platform modules.

    Must run OFF the event loop. Python's import machinery hits the filesystem
    (it scans sys.path — which includes /config/deps — and reads dist-info
    metadata), which HA's blocking-call detector flags and which stalls the
    loop for over a second. HA imports custom-integration modules in its
    dedicated import executor, so doing this at module scope keeps it off the
    loop without async_setup having to await anything.
    """
    from kasa.feature import Feature
    from kasa.iot.modules.motion import Motion

    from homeassistant.components.tplink import binary_sensor as tplink_bs
    from homeassistant.components.tplink import button as tplink_button
    from homeassistant.components.tplink import entity as tplink_entity
    from homeassistant.components.tplink import number as tplink_number
    from homeassistant.components.tplink import select as tplink_select
    from homeassistant.components.tplink import sensor as tplink_sensor

    return _Deps(
        Feature=Feature,
        Motion=Motion,
        tplink_bs=tplink_bs,
        tplink_button=tplink_button,
        tplink_entity=tplink_entity,
        tplink_number=tplink_number,
        tplink_select=tplink_select,
        tplink_sensor=tplink_sensor,
    )


try:
    _DEPS: _Deps | None = _load_deps()
except ImportError as err:
    # tplink's requirements (python-kasa) may not be installed yet the very
    # first time HA imports this module. async_setup retries.
    _LOGGER.debug("tplink_pir_patch: deferring import of kasa/tplink (%s)", err)
    _DEPS = None


async def _safe_reboot(self) -> None:
    """Reboot the switch, then re-arm the PIR.

    A reboot silently disarms the motion sensor. `smartlife.iot.PIR get_config`
    still reports `enable: 1` and HA still shows the Motion sensor switch as on,
    but the device no longer reacts to motion — its built-in Smart Control stops
    driving the load, and pir_triggered stops firing. Toggling the sensor off and
    on again restores it. That is true of a reboot from any source: this action,
    the stock `reboot` button, or the physical reset button on the switch.

    So the sequence is reboot, wait for the device to answer again, then toggle
    `set_enabled` off and back on. The re-arm is skipped when the PIR was already
    disabled, but the wait is not — coming back is what tells us the reboot
    worked, and a switch that never returns is the failure worth logging.

    This blocks for as long as the switch takes to come back, up to
    _REBOOT_WAIT seconds. A button press in the UI will spin for that long.
    """
    dev = self._device
    try:
        was_enabled = bool(self.enabled)
    except Exception:  # noqa: BLE001 - module data may not be populated yet
        # Default to no re-arm: wrongly enabling a PIR the user turned off is
        # worse than skipping a re-arm, which this warning tells them to redo.
        was_enabled = False
        _LOGGER.warning(
            "tplink_pir_patch: could not read PIR state of %s; rebooting without "
            "re-arming — check its Motion sensor switch afterwards",
            dev.host,
            exc_info=True,
        )
    _LOGGER.info(
        "tplink_pir_patch: safe restart of %s (pir_enabled=%s)", dev.host, was_enabled
    )
    try:
        await dev.reboot(delay=1)
    except Exception:  # noqa: BLE001 - device drops the connection mid-reboot
        _LOGGER.debug(
            "tplink_pir_patch: reboot call raised for %s (expected)",
            dev.host,
            exc_info=True,
        )

    deadline = time.monotonic() + _REBOOT_WAIT
    await asyncio.sleep(_REBOOT_SETTLE)
    while time.monotonic() < deadline:
        try:
            await dev.update()
            break
        except Exception:  # noqa: BLE001 - still rebooting / reassociating
            await asyncio.sleep(3)
    else:
        _LOGGER.warning(
            "tplink_pir_patch: %s did not answer within %ss after reboot%s",
            dev.host,
            _REBOOT_WAIT,
            "; PIR left disarmed — toggle its Motion sensor switch off and on"
            if was_enabled
            else "",
        )
        return

    if not was_enabled:
        _LOGGER.info(
            "tplink_pir_patch: %s rebooted (PIR disabled, nothing to re-arm)", dev.host
        )
        return

    try:
        await self.set_enabled(False)
        await asyncio.sleep(_PIR_TOGGLE_GAP)
        await self.set_enabled(True)
    except Exception:
        _LOGGER.exception(
            "tplink_pir_patch: failed to re-arm PIR on %s — toggle its Motion "
            "sensor switch off and on",
            dev.host,
        )
    else:
        _LOGGER.info("tplink_pir_patch: %s rebooted and PIR re-armed", dev.host)


def _patch_kasa_motion(deps: _Deps) -> None:
    Feature = deps.Feature
    Motion = deps.Motion

    if getattr(Motion._initialize_features, "_pir_patched", False):
        return

    Motion.safe_reboot = _safe_reboot

    _original = Motion._initialize_features

    def _patched(self) -> None:
        _original(self)
        # Add pir_cold_time (python-kasa exposes the property + setter but no Feature)
        if "pir_cold_time" not in self._module_features:
            try:
                self._add_feature(
                    Feature(
                        device=self._device,
                        container=self,
                        id="pir_cold_time",
                        name="Motion Inactivity Timeout",
                        icon="mdi:timer-cog-outline",
                        attribute_getter="inactivity_timeout",
                        attribute_setter="set_inactivity_timeout",
                        type=Feature.Type.Number,
                        category=Feature.Category.Config,
                        range_getter=lambda: (5000, 1800000),
                        unit_getter=lambda: "ms",
                    )
                )
            except Exception:
                _LOGGER.exception("tplink_pir_patch: failed to add pir_cold_time")
        # Reboot that re-arms the PIR afterwards. Registered on the Motion
        # module rather than the device so it only appears on PIR-capable
        # switches — the stock `reboot` feature (Category.Debug, so disabled by
        # default in HA) exists on every Kasa iot device and is left alone.
        if "reboot_safe" not in self._module_features:
            try:
                self._add_feature(
                    Feature(
                        device=self._device,
                        container=self,
                        id="reboot_safe",
                        name="Safe restart",
                        icon="mdi:restart-alert",
                        attribute_setter="safe_reboot",
                        type=Feature.Type.Action,
                        category=Feature.Category.Config,
                    )
                )
            except Exception:
                _LOGGER.exception("tplink_pir_patch: failed to add reboot_safe")
        # Flip pir_triggered Sensor -> BinarySensor so HA wires it to the
        # binary_sensor platform with device_class=motion.
        if "pir_triggered" in self._module_features:
            self._module_features["pir_triggered"].type = Feature.Type.BinarySensor
        # python-kasa registers pir_range with attribute_getter="range" returning
        # a Range enum, but choices_getter="ranges" returns string names. HA's
        # SelectEntity compares feature.value (enum) to options (strings) and
        # shows "Unknown" because they never match. Wrap the getter to return
        # the enum's name string.
        if "pir_range" in self._module_features:
            self._module_features["pir_range"].attribute_getter = (
                lambda container: container.range.name
            )

    _patched._pir_patched = True
    Motion._initialize_features = _patched


def _patch_ha_tplink(deps: _Deps) -> None:
    tplink_bs = deps.tplink_bs
    tplink_button = deps.tplink_button
    tplink_entity = deps.tplink_entity
    tplink_number = deps.tplink_number
    tplink_select = deps.tplink_select
    tplink_sensor = deps.tplink_sensor

    sensor_descs = (
        tplink_sensor.TPLinkSensorEntityDescription(
            key="pir_value",
            name="PIR value",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        tplink_sensor.TPLinkSensorEntityDescription(
            key="pir_adc_value",
            name="PIR ADC value",
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=False,
        ),
        tplink_sensor.TPLinkSensorEntityDescription(
            key="pir_adc_min", name="PIR ADC min",
            entity_registry_enabled_default=False,
        ),
        tplink_sensor.TPLinkSensorEntityDescription(
            key="pir_adc_mid", name="PIR ADC mid",
            entity_registry_enabled_default=False,
        ),
        tplink_sensor.TPLinkSensorEntityDescription(
            key="pir_adc_max", name="PIR ADC max",
            entity_registry_enabled_default=False,
        ),
        tplink_sensor.TPLinkSensorEntityDescription(
            key="pir_percent",
            name="PIR percentile",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=False,
        ),
    )
    for d in sensor_descs:
        tplink_sensor.SENSOR_DESCRIPTIONS_MAP.setdefault(d.key, d)

    bs_desc = tplink_bs.TPLinkBinarySensorEntityDescription(
        key="pir_triggered",
        device_class=BinarySensorDeviceClass.MOTION,
    )
    tplink_bs.BINARYSENSOR_DESCRIPTIONS_MAP.setdefault(bs_desc.key, bs_desc)

    number_descs = (
        tplink_number.TPLinkNumberEntityDescription(
            key="pir_cold_time",
            name="Motion inactivity timeout",
            mode=NumberMode.BOX,
            device_class=NumberDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        ),
        # Higher value = larger ADC deviation required to trigger = less sensitive.
        # Picking a preset via select.pir_range overwrites this; setting it
        # manually switches the device to Range.Custom.
        tplink_number.TPLinkNumberEntityDescription(
            key="pir_threshold",
            name="Motion sensor threshold",
            mode=NumberMode.SLIDER,
            native_unit_of_measurement=PERCENTAGE,
        ),
    )
    for d in number_descs:
        tplink_number.NUMBER_DESCRIPTIONS_MAP.setdefault(d.key, d)

    select_desc = tplink_select.TPLinkSelectEntityDescription(
        key="pir_range",
        name="Motion sensor range",
    )
    tplink_select.SELECT_DESCRIPTIONS_MAP.setdefault(select_desc.key, select_desc)

    button_desc = tplink_button.TPLinkButtonEntityDescription(
        key="reboot_safe",
        name="Safe restart",
        device_class=ButtonDeviceClass.RESTART,
    )
    tplink_button.BUTTON_DESCRIPTIONS_MAP.setdefault(button_desc.key, button_desc)

    # pir_triggered is Feature.Category.Primary; on Dimmer the integration filters
    # Primary features unless explicitly allowlisted.
    tplink_entity.FEATURES_ALLOW_LIST.add("pir_triggered")

    # HA's tplink integration calls dataclasses.replace(desc, name=UNDEFINED) on
    # every description, clobbering any explicit name we set above. For features
    # with no strings.json translation (all our pir_* keys), this leaves the
    # entity with no name suffix — UI shows only the device name.
    # Wrap _description_for_feature to preserve the input description's name.
    feat_entity_cls = tplink_entity.CoordinatedTPLinkFeatureEntity
    if not getattr(feat_entity_cls._description_for_feature, "_pir_name_patched", False):
        _orig_dff = feat_entity_cls._description_for_feature

        def _patched_dff(cls, feature, descriptions, *, device, parent=None):
            input_name = UNDEFINED
            if descriptions and (input_desc := descriptions.get(feature.id)):
                input_name = input_desc.name
            desc = _orig_dff(feature, descriptions, device=device, parent=parent)
            if desc is not None and input_name is not UNDEFINED:
                desc = dataclasses.replace(desc, name=input_name)
            return desc

        _patched_dff._pir_name_patched = True
        feat_entity_cls._description_for_feature = classmethod(_patched_dff)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    # Deliberately await-free: tplink is set up concurrently in the same
    # bootstrap stage, and its platforms read the description maps we mutate
    # here. Yielding to the event loop turns that ordering into a race — an
    # awaited import executor job measured a 36 ms margin, versus 17 s when
    # the patches are applied without yielding. Imports happen at module
    # scope (in HA's import executor) for exactly this reason.
    deps = _DEPS
    if deps is None:
        try:
            # Rare: kasa was not importable when this module loaded. Retrying
            # here blocks the loop briefly, which beats not loading at all.
            deps = _load_deps()
        except ImportError:
            _LOGGER.exception(
                "tplink_pir_patch: python-kasa / tplink not importable; "
                "is the TP-Link integration installed?"
            )
            return False
    try:
        _patch_kasa_motion(deps)
        _patch_ha_tplink(deps)
    except Exception:
        _LOGGER.exception("tplink_pir_patch: setup failed")
        return False
    _LOGGER.info("tplink_pir_patch: kasa Motion + HA tplink descriptions patched")
    return True
