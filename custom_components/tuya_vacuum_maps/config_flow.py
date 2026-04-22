"""Handle Home Assistant config flow for the Tuya Vacuum Maps integration."""

import logging
from typing import Any, override

import tuya_vacuum
from tuya_vacuum.tuya import (
    CrossRegionAccessError,
    InvalidClientIDError,
    InvalidClientSecretError,
    InvalidDeviceIDError,
)
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_DEVICE_ID,
    CONF_NAME,
)

from .const import CONF_SERVER, CONF_SERVER_WEST_AMERICA, CONF_SERVERS, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _create_vacuum(data: dict[str, Any]) -> Any:
    """Create a vacuum object for supported tuya_vacuum versions."""
    vacuum_cls = getattr(tuya_vacuum, "TuyaVacuum", None)
    if vacuum_cls is None:
        vacuum_cls = getattr(tuya_vacuum, "Vacuum", None)
    if vacuum_cls is None:
        raise AttributeError("tuya_vacuum has no TuyaVacuum or Vacuum class")

    return vacuum_cls(
        data["server"], data["client_id"], data["client_secret"], data["device_id"]
    )


def _fetch_realtime_map(vacuum: Any) -> Any:
    """Fetch realtime map across library API variants."""
    for method_name in (
        "fetch_realtime_map",
        "get_realtime_map",
        "get_realtime_maps",
        "fetch_map",
        "get_map",
        "realtime_map",
    ):
        method = getattr(vacuum, method_name, None)
        if callable(method):
            result = method()
            if isinstance(result, list):
                if not result:
                    raise ValueError("Map request returned an empty list")
                return result[0]
            return result

    available = [n for n in dir(vacuum) if "map" in n.lower() or "fetch" in n.lower()]
    raise AttributeError(
        f"{type(vacuum).__name__} has no supported map method. Available: {available}"
    )


def _validate_input_sync(data: dict[str, Any]) -> None:
    """Validate credentials and map access using blocking library calls."""
    vacuum = _create_vacuum(data)
    _fetch_realtime_map(vacuum)


async def validate_input(hass, data: dict[str, Any]) -> None:
    """Validate that the user input allows us to connect."""
    await hass.async_add_executor_job(_validate_input_sync, data)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya Vacuum Maps."""

    VERSION = 1
    MINOR_VERSION = 1

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the user step."""
        errors = {}

        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)

                return self.async_create_entry(
                    title=user_input.pop(CONF_NAME), data=user_input
                )
            except CrossRegionAccessError:
                errors[CONF_SERVER] = (
                    "Cross region access is not allowed, data center mismatch."
                )
            except InvalidClientIDError:
                errors[CONF_CLIENT_ID] = "Invalid Client ID."
            except InvalidClientSecretError:
                errors[CONF_CLIENT_SECRET] = "Invalid Client Secret."
            except InvalidDeviceIDError:
                errors[CONF_DEVICE_ID] = "Invalid Device ID."
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error("Error occurred while validating: %s", err)
                err_text = str(err)
                if "Map layout version" in err_text and "is not supported" in err_text:
                    errors["base"] = (
                        "Map layout not supported by installed tuya-vacuum version. "
                        "Update integration dependencies."
                    )
                else:
                    errors["base"] = err_text or "Unknown error occurred."

        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Vacuum Map"): str,
                vol.Required(CONF_SERVER, default=CONF_SERVER_WEST_AMERICA): vol.In(
                    CONF_SERVERS
                ),
                vol.Required(CONF_CLIENT_ID, default=""): str,
                vol.Required(CONF_CLIENT_SECRET, default=""): str,
                vol.Required(CONF_DEVICE_ID, default=""): str,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )
