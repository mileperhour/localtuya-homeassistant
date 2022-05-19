"""Config flow for LocalTuya integration integration."""
import errno
import logging
import time
from importlib import import_module

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries, core, exceptions
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_HOST,
    CONF_ID,
    CONF_MODEL,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_SCAN_INTERVAL,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REGION,
    CONF_USERNAME,
)
from homeassistant.core import callback

from .cloud_api import TuyaCloudApi
from .common import async_config_entry_by_device_id, pytuya
from .const import (
    CONF_ACTION,
    CONF_ADD_DEVICE,
    CONF_EDIT_DEVICE,
    CONF_SETUP_CLOUD,
    CONF_LOCAL_KEY,
    CONF_PRODUCT_NAME,
    CONF_PROTOCOL_VERSION,
    CONF_USER_ID,
    CONF_DPS_STRINGS,
    ATTR_UPDATED_AT,
    DATA_DISCOVERY,
    DATA_CLOUD,
    DOMAIN,
    PLATFORMS,
)
from .discovery import discover

_LOGGER = logging.getLogger(__name__)

PLATFORM_TO_ADD = "platform_to_add"
NO_ADDITIONAL_ENTITIES = "no_additional_entities"
SELECTED_DEVICE = "selected_device"

CUSTOM_DEVICE = "..."

CONF_ACTIONS = {
    CONF_ADD_DEVICE: "Add a new device",
    CONF_EDIT_DEVICE: "Edit a device",
    CONF_SETUP_CLOUD: "Reconfigure Cloud API account",
}

CONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACTION, default=CONF_ADD_DEVICE): vol.In(CONF_ACTIONS),
    }
)

CLOUD_SETUP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REGION, default="eu"): vol.In(["eu", "us", "cn", "in"]),
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
        vol.Required(CONF_USER_ID): cv.string,
        vol.Optional(CONF_USERNAME, default=DOMAIN): cv.string,
    }
)

CONFIGURE_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FRIENDLY_NAME): str,
        vol.Required(CONF_LOCAL_KEY): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_DEVICE_ID): str,
        vol.Required(CONF_PROTOCOL_VERSION, default="3.3"): vol.In(["3.1", "3.3"]),
        vol.Optional(CONF_SCAN_INTERVAL): int,
    }
)

DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_LOCAL_KEY): cv.string,
        vol.Required(CONF_FRIENDLY_NAME): cv.string,
        vol.Required(CONF_PROTOCOL_VERSION, default="3.3"): vol.In(["3.1", "3.3"]),
        vol.Optional(CONF_SCAN_INTERVAL): int,
    }
)

PICK_ENTITY_SCHEMA = vol.Schema(
    {vol.Required(PLATFORM_TO_ADD, default="switch"): vol.In(PLATFORMS)}
)


def devices_schema(discovered_devices, cloud_devices_list, add_custom_device=True):
    """Create schema for devices step."""
    devices = {}
    for dev_id, dev_host in discovered_devices.items():
        dev_name = dev_id
        if dev_id in cloud_devices_list.keys():
            dev_name = cloud_devices_list[dev_id][CONF_NAME]
        devices[dev_id] = f"{dev_name} ({dev_host})"

    if add_custom_device:
        devices.update({CUSTOM_DEVICE: CUSTOM_DEVICE})

    # devices.update(
    #     {
    #         ent.data[CONF_DEVICE_ID]: ent.data[CONF_FRIENDLY_NAME]
    #         for ent in entries
    #     }
    # )
    return vol.Schema({vol.Required(SELECTED_DEVICE): vol.In(devices)})


def options_schema(entities):
    """Create schema for options."""
    entity_names = [
        f"{entity[CONF_ID]}: {entity[CONF_FRIENDLY_NAME]}" for entity in entities
    ]
    return vol.Schema(
        {
            vol.Required(CONF_FRIENDLY_NAME): str,
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_LOCAL_KEY): str,
            vol.Required(CONF_PROTOCOL_VERSION, default="3.3"): vol.In(["3.1", "3.3"]),
            vol.Optional(CONF_SCAN_INTERVAL): int,
            vol.Required(
                CONF_ENTITIES, description={"suggested_value": entity_names}
            ): cv.multi_select(entity_names),
        }
    )


def schema_defaults(schema, dps_list=None, **defaults):
    """Create a new schema with default values filled in."""
    copy = schema.extend({})
    for field, field_type in copy.schema.items():
        if isinstance(field_type, vol.In):
            value = None
            for dps in dps_list or []:
                if dps.startswith(f"{defaults.get(field)} "):
                    value = dps
                    break

            if value in field_type.container:
                field.default = vol.default_factory(value)
                continue

        if field.schema in defaults:
            field.default = vol.default_factory(defaults[field])
    return copy


def dps_string_list(dps_data):
    """Return list of friendly DPS values."""
    return [f"{id} (value: {value})" for id, value in dps_data.items()]


def gen_dps_strings():
    """Generate list of DPS values."""
    return [f"{dp} (value: ?)" for dp in range(1, 256)]


def platform_schema(platform, dps_strings, allow_id=True, yaml=False):
    """Generate input validation schema for a platform."""
    schema = {}
    if yaml:
        # In YAML mode we force the specified platform to match flow schema
        schema[vol.Required(CONF_PLATFORM)] = vol.In([platform])
    if allow_id:
        schema[vol.Required(CONF_ID)] = vol.In(dps_strings)
    schema[vol.Required(CONF_FRIENDLY_NAME)] = str
    return vol.Schema(schema).extend(flow_schema(platform, dps_strings))


def flow_schema(platform, dps_strings):
    """Return flow schema for a specific platform."""
    integration_module = ".".join(__name__.split(".")[:-1])
    return import_module("." + platform, integration_module).flow_schema(dps_strings)


def strip_dps_values(user_input, dps_strings):
    """Remove values and keep only index for DPS config items."""
    stripped = {}
    for field, value in user_input.items():
        if value in dps_strings:
            stripped[field] = int(user_input[field].split(" ")[0])
        else:
            stripped[field] = user_input[field]
    return stripped


def config_schema():
    """Build schema used for setting up component."""
    entity_schemas = [
        platform_schema(platform, range(1, 256), yaml=True) for platform in PLATFORMS
    ]
    return vol.Schema(
        {
            DOMAIN: vol.All(
                cv.ensure_list,
                [
                    DEVICE_SCHEMA.extend(
                        {vol.Required(CONF_ENTITIES): [vol.Any(*entity_schemas)]}
                    )
                ],
            )
        },
        extra=vol.ALLOW_EXTRA,
    )


async def validate_input(hass: core.HomeAssistant, data):
    """Validate the user input allows us to connect."""
    detected_dps = {}

    interface = None
    try:
        interface = await pytuya.connect(
            data[CONF_HOST],
            data[CONF_DEVICE_ID],
            data[CONF_LOCAL_KEY],
            float(data[CONF_PROTOCOL_VERSION]),
        )

        detected_dps = await interface.detect_available_dps()
    except (ConnectionRefusedError, ConnectionResetError) as ex:
        raise CannotConnect from ex
    except ValueError as ex:
        raise InvalidAuth from ex
    finally:
        if interface:
            await interface.close()

    # Indicate an error if no datapoints found as the rest of the flow
    # won't work in this case
    if not detected_dps:
        raise EmptyDpsList

    return dps_string_list(detected_dps)


async def attempt_cloud_connection(hass, user_input):
    """Create device."""
    tuya_api = TuyaCloudApi(
        hass,
        user_input.get(CONF_REGION),
        user_input.get(CONF_CLIENT_ID),
        user_input.get(CONF_CLIENT_SECRET),
        user_input.get(CONF_USER_ID),
    )

    res = await tuya_api.async_get_access_token()
    _LOGGER.debug("ACCESS TOKEN RES: %s", res)
    if res != "ok":
        return {"reason": "authentication_failed", "msg": res}

    res = await tuya_api.async_get_devices_list()
    _LOGGER.debug("DEV LIST RES: %s", res)
    if res != "ok":
        return {"reason": "device_list_failed", "msg": res}

    for dev_id, dev in tuya_api._device_list.items():
        print(f"Name: {dev['name']} \t dev_id {dev['id']} \t key {dev['local_key']} ")

    return {}


class LocaltuyaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for LocalTuya integration."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get options flow for this handler."""
        return LocalTuyaOptionsFlowHandler(config_entry)

    def __init__(self):
        """Initialize a new LocaltuyaConfigFlow."""

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        placeholders = {}
        if user_input is not None:
            print("ECCOCI")
            res = await attempt_cloud_connection(self.hass, user_input)

            if len(res) == 0:
                return await self._create_entry(user_input)
            errors["base"] = res["reason"]
            placeholders = {"msg": res["msg"]}

        defaults = {}
        defaults[CONF_REGION] = "eu"
        defaults[CONF_CLIENT_ID] = "xxx"
        defaults[CONF_CLIENT_SECRET] = "xxx"
        defaults[CONF_USER_ID] = "xxx"
        defaults[CONF_USERNAME] = "xxx"
        defaults.update(user_input or {})

        return self.async_show_form(
            step_id="user",
            data_schema=schema_defaults(CLOUD_SETUP_SCHEMA, **defaults),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _create_entry(self, user_input):
        """Register new entry."""
        # if not self.unique_id:
        #    await self.async_set_unique_id(password)
        # self._abort_if_unique_id_configured()
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(user_input.get(CONF_USER_ID))
        user_input[CONF_DEVICES] = {}

        return self.async_create_entry(
            title=user_input.get(CONF_USERNAME),
            data=user_input,
        )

    async def async_step_import(self, user_input):
        """Handle import from YAML."""
        _LOGGER.error(
            "Configuration via YAML file is no longer supported by this integration."
        )
        # await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
        # self._abort_if_unique_id_configured(updates=user_input)
        # return self.async_create_entry(
        #     title=f"{user_input[CONF_FRIENDLY_NAME]} (YAML)", data=user_input
        # )


class LocalTuyaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for LocalTuya integration."""

    def __init__(self, config_entry):
        """Initialize localtuya options flow."""
        self.config_entry = config_entry
        # self.dps_strings = config_entry.data.get(CONF_DPS_STRINGS, gen_dps_strings())
        # self.entities = config_entry.data[CONF_ENTITIES]
        self.selected_device = None
        self.editing_device = False
        self.device_data = None
        self.dps_strings = []
        self.selected_platform = None
        self.discovered_devices = {}
        self.entities = []

    async def async_step_init(self, user_input=None):
        """Manage basic options."""
        # device_id = self.config_entry.data[CONF_DEVICE_ID]
        if user_input is not None:
            if user_input.get(CONF_ACTION) == CONF_SETUP_CLOUD:
                return await self.async_step_cloud_setup()
            if user_input.get(CONF_ACTION) == CONF_ADD_DEVICE:
                return await self.async_step_add_device()
            if user_input.get(CONF_ACTION) == CONF_EDIT_DEVICE:
                return await self.async_step_edit_device()

        return self.async_show_form(
            step_id="init",
            data_schema=CONFIGURE_SCHEMA,
        )

    async def async_step_cloud_setup(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        placeholders = {}
        if user_input is not None:
            res = await attempt_cloud_connection(self.hass, user_input)

            if len(res) == 0:
                new_data = self.config_entry.data.copy()
                new_data.update(user_input)
                print("CURR_ENTRY {}".format(self.config_entry))
                print("NEW DATA {}".format(new_data))

                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )
                return self.async_create_entry(title="", data={})
            errors["base"] = res["reason"]
            placeholders = {"msg": res["msg"]}

        defaults = self.config_entry.data.copy()
        defaults.update(user_input or {})

        return self.async_show_form(
            step_id="cloud_setup",
            data_schema=schema_defaults(CLOUD_SETUP_SCHEMA, **defaults),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_add_device(self, user_input=None):
        """Handle adding a new device."""
        # Use cache if available or fallback to manual discovery
        self.editing_device = False
        errors = {}
        if user_input is not None:
            print("Selected {}".format(user_input))
            if user_input[SELECTED_DEVICE] != CUSTOM_DEVICE:
                self.selected_device = user_input[SELECTED_DEVICE]
            return await self.async_step_configure_device()

        self.discovered_devices = {}
        data = self.hass.data.get(DOMAIN)

        if data and DATA_DISCOVERY in data:
            self.discovered_devices = data[DATA_DISCOVERY].devices
        else:
            try:
                self.discovered_devices = await discover()
            except OSError as ex:
                if ex.errno == errno.EADDRINUSE:
                    errors["base"] = "address_in_use"
                else:
                    errors["base"] = "discovery_failed"
            except Exception:  # pylint: disable= broad-except
                _LOGGER.exception("discovery failed")
                errors["base"] = "discovery_failed"

        devices = {
            dev_id: dev["ip"]
            for dev_id, dev in self.discovered_devices.items()
            if dev["gwId"] not in self.config_entry.data[CONF_DEVICES]
        }
        print("SCHEMA DEVS {}".format(devices))

        return self.async_show_form(
            step_id="add_device",
            data_schema=devices_schema(
                devices, self.hass.data[DOMAIN][DATA_CLOUD]._device_list
            ),
            errors=errors,
        )

    async def async_step_edit_device(self, user_input=None):
        """Handle editing a device."""
        self.editing_device = True
        # Use cache if available or fallback to manual discovery
        print("AAA")
        errors = {}
        if user_input is not None:
            print("Selected {}".format(user_input))
            self.selected_device = user_input[SELECTED_DEVICE]
            dev_conf = self.config_entry.data[CONF_DEVICES][self.selected_device]
            self.dps_strings = dev_conf.get(CONF_DPS_STRINGS, gen_dps_strings())
            self.entities = dev_conf[CONF_ENTITIES]
            print("Selected  DPS {} ENT {}".format(self.dps_strings, self.entities))

            return await self.async_step_configure_device()

        print("BBB: {}".format(self.config_entry.data[CONF_DEVICES]))
        devices = {}
        for dev_id, configured_dev in self.config_entry.data[CONF_DEVICES].items():
            devices[dev_id] = configured_dev[CONF_HOST]
        print("SCHEMA DEVS {}".format(devices))

        return self.async_show_form(
            step_id="edit_device",
            data_schema=devices_schema(
                devices, self.hass.data[DOMAIN][DATA_CLOUD]._device_list, False
            ),
            errors=errors,
        )

    async def async_step_configure_device(self, user_input=None):
        """Handle input of basic info."""
        errors = {}
        dev_id = self.selected_device
        if user_input is not None:
            print("INPUT1!! {} {}".format(user_input, dev_id))

            try:
                self.device_data = user_input.copy()
                if dev_id is not None:
                    # self.device_data[CONF_PRODUCT_KEY] = self.devices[
                    #     self.selected_device
                    # ]["productKey"]
                    cloud_devs = self.hass.data[DOMAIN][DATA_CLOUD]._device_list
                    if dev_id in cloud_devs:
                        self.device_data[CONF_MODEL] = cloud_devs[dev_id].get(
                            CONF_PRODUCT_NAME
                        )
                if self.editing_device:
                    self.device_data.update(
                        {
                            CONF_DEVICE_ID: dev_id,
                            CONF_MODEL: self.device_data[CONF_MODEL],
                            CONF_DPS_STRINGS: self.dps_strings,
                            CONF_ENTITIES: [],
                        }
                    )
                    if len(user_input[CONF_ENTITIES]) > 0:
                        entity_ids = [
                            int(entity.split(":")[0])
                            for entity in user_input[CONF_ENTITIES]
                        ]
                        device_config = self.config_entry.data[CONF_DEVICES][dev_id]
                        self.entities = [
                            entity
                            for entity in device_config[CONF_ENTITIES]
                            if entity[CONF_ID] in entity_ids
                        ]
                        return await self.async_step_configure_entity()

                self.dps_strings = await validate_input(self.hass, user_input)
                print("ZIO KEN!! {} ".format(self.dps_strings))
                return await self.async_step_pick_entity_type()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except EmptyDpsList:
                errors["base"] = "empty_dps"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        defaults = {}
        if self.editing_device:
            # If selected device exists as a config entry, load config from it
            print("ALREADY EXISTING!! {}".format(dev_id))
            defaults = self.config_entry.data[CONF_DEVICES][dev_id].copy()
            print("ALREADY EXISTING!! {} {}".format(self.entities, defaults))
            schema = schema_defaults(options_schema(self.entities), **defaults)
            placeholders = {"for_device": f" for device `{dev_id}`"}
            print("SCHEMA!! {}".format(schema))
        elif dev_id is not None:
            # Insert default values from discovery and cloud if present
            print("NEW DEVICE!! {}".format(dev_id))
            device = self.discovered_devices[dev_id]
            defaults[CONF_HOST] = device.get("ip")
            defaults[CONF_DEVICE_ID] = device.get("gwId")
            defaults[CONF_PROTOCOL_VERSION] = device.get("version")
            cloud_devs = self.hass.data[DOMAIN][DATA_CLOUD]._device_list
            if dev_id in cloud_devs:
                defaults[CONF_LOCAL_KEY] = cloud_devs[dev_id].get(CONF_LOCAL_KEY)
                defaults[CONF_FRIENDLY_NAME] = cloud_devs[dev_id].get(CONF_NAME)
            schema = schema_defaults(CONFIGURE_DEVICE_SCHEMA, **defaults)
            placeholders = {"for_device": ""}
        defaults.update(user_input or {})

        return self.async_show_form(
            step_id="configure_device",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_pick_entity_type(self, user_input=None):
        """Handle asking if user wants to add another entity."""
        if user_input is not None:
            print("INPUT4!! {} {}".format(user_input, self.device_data))
            if user_input.get(NO_ADDITIONAL_ENTITIES):
                config = {
                    **self.device_data,
                    CONF_DPS_STRINGS: self.dps_strings,
                    CONF_ENTITIES: self.entities,
                }
                print("NEW CONFIG!! {}".format(config))

                # entry = async_config_entry_by_device_id(self.hass, self.unique_id)
                dev_id = self.device_data.get(CONF_DEVICE_ID)
                if dev_id in self.config_entry.data[CONF_DEVICES]:
                    print("AGGIORNO !! {}".format(dev_id))
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data=config
                    )
                    return self.async_abort(
                        reason="device_success",
                        description_placeholders={
                            "dev_name": config.get(CONF_FRIENDLY_NAME),
                            "action": "updated",
                        },
                    )

                print("CREO NUOVO DEVICE!! {}".format(dev_id))
                new_data = self.config_entry.data.copy()
                print("PRE:     {}".format(new_data))
                new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))
                # new_data[CONF_DEVICES]["AZZ"] = "OK"
                new_data[CONF_DEVICES].update({dev_id: config})
                print("POST:    {}".format(new_data))

                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )
                return self.async_create_entry(title="", data={})
                self.async_create_entry(title="", data={})

            self.selected_platform = user_input[PLATFORM_TO_ADD]
            return await self.async_step_configure_entity()

        # Add a checkbox that allows bailing out from config flow if at least one
        # entity has been added
        schema = PICK_ENTITY_SCHEMA
        if self.selected_platform is not None:
            schema = schema.extend(
                {vol.Required(NO_ADDITIONAL_ENTITIES, default=True): bool}
            )

        return self.async_show_form(step_id="pick_entity_type", data_schema=schema)

    def available_dps_strings(self):
        available_dps = []
        # print("FILTERING!! {} {}".format(self.dps_strings, self.entities))
        used_dps = [str(entity[CONF_ID]) for entity in self.entities]
        # print("FILTERING-- {} {}".format(self.dps_strings, used_dps))
        for dp_string in self.dps_strings:
            dp = dp_string.split(" ")[0]
            # print("FILTERING2!! {} {}".format(dp_string, dp))
            if dp not in used_dps:
                available_dps.append(dp_string)
        return available_dps

    async def async_step_entity(self, user_input=None):
        """Manage entity settings."""
        errors = {}
        if user_input is not None:
            print("INPUT2!! {} {}".format(user_input, CONF_DEVICE_ID))
            print("ZIO KEN!! {} ".format(self.dps_strings))
            entity = strip_dps_values(user_input, self.dps_strings)
            entity[CONF_ID] = self.current_entity[CONF_ID]
            entity[CONF_PLATFORM] = self.current_entity[CONF_PLATFORM]
            self.device_data[CONF_ENTITIES].append(entity)

            if len(self.entities) == len(self.device_data[CONF_ENTITIES]):
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    title=self.device_data[CONF_FRIENDLY_NAME],
                    data=self.device_data,
                )
                return self.async_create_entry(title="", data={})

        schema = platform_schema(
            self.current_entity[CONF_PLATFORM], self.dps_strings, allow_id=False
        )
        return self.async_show_form(
            step_id="entity",
            errors=errors,
            data_schema=schema_defaults(
                schema, self.dps_strings, **self.current_entity
            ),
            description_placeholders={
                "id": self.current_entity[CONF_ID],
                "platform": self.current_entity[CONF_PLATFORM],
            },
        )

    async def async_step_configure_entity(self, user_input=None):
        """Manage entity settings."""
        errors = {}
        if user_input is not None:
            print("INPUT3!! {} {}".format(user_input, CONF_DEVICE_ID))
            already_configured = any(
                entity[CONF_ID] == int(user_input[CONF_ID].split(" ")[0])
                for entity in self.entities
            )
            if not already_configured:
                user_input[CONF_PLATFORM] = self.selected_platform
                self.entities.append(strip_dps_values(user_input, self.dps_strings))
                # new entity added. Let's check if there are more left...
                print("ADDED. Remaining... {}".format(self.available_dps_strings()))
                user_input = None
                if len(self.available_dps_strings()) == 0:
                    user_input = {NO_ADDITIONAL_ENTITIES: True}
                return await self.async_step_pick_entity_type(user_input)

            errors["base"] = "entity_already_configured"

        if self.editing_device:
            schema = platform_schema(
                self.current_entity[CONF_PLATFORM], self.dps_strings, allow_id=False
            )
            schema = schema_defaults(schema, self.dps_strings, **self.current_entity)
            placeholders = {
                "entity": f"entity with DP {self.current_entity[CONF_ID]}",
                "platform": self.current_entity[CONF_PLATFORM],
            }
        else:
            available_dps = self.available_dps_strings()
            schema = platform_schema(self.selected_platform, available_dps)
            placeholders = {
                "entity": "an entity",
                "platform": self.selected_platform,
            }

        return self.async_show_form(
            step_id="configure_entity",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_yaml_import(self, user_input=None):
        """Manage YAML imports."""
        _LOGGER.error(
            "Configuration via YAML file is no longer supported by this integration."
        )
        # if user_input is not None:
        #     return self.async_create_entry(title="", data={})
        # return self.async_show_form(step_id="yaml_import")

    @property
    def current_entity(self):
        """Existing configuration for entity currently being edited."""
        return self.entities[len(self.device_data[CONF_ENTITIES])]


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""


class EmptyDpsList(exceptions.HomeAssistantError):
    """Error to indicate no datapoints found."""
