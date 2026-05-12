"""Surface python-kasa PIR features as proper HA entities.

For ES20M / KS200M motion-sensor switches (iot module). Adds:
  - binary_sensor pir_triggered (device_class=motion) — flipped from python-kasa's Sensor
  - sensor pir_value / pir_percent / pir_adc_* (debug ones disabled by default)
  - number pir_cold_time — inactivity timeout (python-kasa exposes the setter but no Feature)

Loaded via configuration.yaml (stage 4), runs before tplink config entries (stage 5),
so SENSOR/NUMBER/BINARY_SENSOR description maps are mutated before tplink reads them.
"""
from __future__ import annotations

import logging

import dataclasses

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.number import NumberDeviceClass, NumberMode
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import UNDEFINED, ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "tplink_pir_patch"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


def _patch_kasa_motion() -> None:
    from kasa.feature import Feature
    from kasa.iot.modules.motion import Motion

    if getattr(Motion._initialize_features, "_pir_patched", False):
        return

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


def _patch_ha_tplink() -> None:
    from homeassistant.components.tplink import binary_sensor as tplink_bs
    from homeassistant.components.tplink import entity as tplink_entity
    from homeassistant.components.tplink import number as tplink_number
    from homeassistant.components.tplink import select as tplink_select
    from homeassistant.components.tplink import sensor as tplink_sensor

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
    try:
        _patch_kasa_motion()
        _patch_ha_tplink()
    except Exception:
        _LOGGER.exception("tplink_pir_patch: setup failed")
        return False
    _LOGGER.info("tplink_pir_patch: kasa Motion + HA tplink descriptions patched")
    return True
