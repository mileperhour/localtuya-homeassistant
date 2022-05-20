"""Diagnostics support for LocalTuya."""
from __future__ import annotations
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICES
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN, DATA_CLOUD


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = {}
    data = {**entry.data}
    # print("DATA is {}".format(data))
    tuya_api = hass.data[DOMAIN][DATA_CLOUD]
    data["cloud_devices"] = tuya_api._device_list

    # censoring private information
    # data["token"] = re.sub(r"[^\-]", "*", data["token"])
    # data["userId"] = re.sub(r"[^\-]", "*", data["userId"])
    return data


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device entry."""
    data = {}
    dev_id = list(device.identifiers)[0][1].split("_")[-1]
    data["device_config"] = entry.data[CONF_DEVICES][dev_id]
    tuya_api = hass.data[DOMAIN][DATA_CLOUD]
    if dev_id in tuya_api._device_list:
        data["device_cloud_info"] = tuya_api._device_list[dev_id]
    # data["log"] = hass.data[DOMAIN][CONF_DEVICES][dev_id].logger.retrieve_log()
    return data
