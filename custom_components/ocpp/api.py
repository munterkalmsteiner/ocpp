"""Representation of OCCP Entities."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import logging
from math import sqrt
import ssl
import time

from homeassistant.components.persistent_notification import DOMAIN as PN_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OK, STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_component, entity_registry
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
import websockets.connection
import websockets.server

from ocpp.exceptions import NotImplementedError
from ocpp.messages import CallError
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cp, call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    AvailabilityStatus,
    AvailabilityType,
    ChargePointStatus,
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    Measurand,
    MessageTrigger,
    Phase,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UnitOfMeasure,
    UnlockStatus,
)

from .const import (
    CONF_AUTH_STATUS,
    CONF_CS_ID,
    CONF_FORCE_SMART_CHARGING,
    CONF_HOST,
    CONF_IDLE_INTERVAL,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_PORT,
    CONF_SKIP_SCHEMA_VALIDATION,
    CONF_SSL,
    CONF_SSL_CERTFILE_PATH,
    CONF_SSL_KEYFILE_PATH,
    CONF_SUBPROTOCOL,
    CONF_WEBSOCKET_CLOSE_TIMEOUT,
    CONF_WEBSOCKET_PING_INTERVAL,
    CONF_WEBSOCKET_PING_TIMEOUT,
    CONF_WEBSOCKET_PING_TRIES,
    DEFAULT_AUTH_STATUS,
    DEFAULT_CS_ID,
    DEFAULT_ENERGY_UNIT,
    DEFAULT_FORCE_SMART_CHARGING,
    DEFAULT_HOST,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_MEASURAND,
    DEFAULT_METER_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_POWER_UNIT,
    DEFAULT_SKIP_SCHEMA_VALIDATION,
    DEFAULT_SSL,
    DEFAULT_SSL_CERTFILE_PATH,
    DEFAULT_SSL_KEYFILE_PATH,
    DEFAULT_SUBPROTOCOL,
    DEFAULT_WEBSOCKET_CLOSE_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_INTERVAL,
    DEFAULT_WEBSOCKET_PING_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_TRIES,
    DEVICE_TYPE_CENTRAL_SYSTEM,
    DEVICE_TYPE_CHARGE_POINT,
    DEVICE_TYPE_TAG,
    DOMAIN,
    HA_ENERGY_UNIT,
    HA_POWER_UNIT,
    UNITS_OCCP_TO_HA,
)
from .enums import (
    ConfigurationKey as ckey,
    HAChargerDetails as cdet,
    HAChargerServices as csvcs,
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
    OcppMisc as om,
    Profiles as prof,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)
# Uncomment these when Debugging
# logging.getLogger("asyncio").setLevel(logging.DEBUG)
# logging.getLogger("websockets").setLevel(logging.DEBUG)

TIME_MINUTES = UnitOfTime.MINUTES

UFW_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("firmware_url"): cv.string,
        vol.Optional("delay_hours"): cv.positive_int,
    }
)
CONF_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("ocpp_key"): cv.string,
        vol.Required("value"): cv.string,
    }
)
GCONF_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("ocpp_key"): cv.string,
    }
)
GDIAG_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("upload_url"): cv.string,
    }
)
TRANS_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("vendor_id"): cv.string,
        vol.Optional("message_id"): cv.string,
        vol.Optional("data"): cv.string,
    }
)
CHRGR_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("limit_amps"): cv.positive_float,
        vol.Optional("limit_watts"): cv.positive_int,
        vol.Optional("conn_id"): cv.positive_int,
        vol.Optional("custom_profile"): vol.Any(cv.string, dict),
    }
)


class CentralSystem:
    """Server for handling OCPP connections."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Instantiate instance of a CentralSystem."""
        self.hass = hass
        self.entry = entry
        self.config = entry.options
        self.host = self.config.get(CONF_HOST, DEFAULT_HOST)
        self.port = self.config.get(CONF_PORT, DEFAULT_PORT)
        self.cs_id = self.config.get(CONF_CS_ID, DEFAULT_CS_ID)
        self.websocket_close_timeout = self.config.get(
            CONF_WEBSOCKET_CLOSE_TIMEOUT, DEFAULT_WEBSOCKET_CLOSE_TIMEOUT
        )
        self.websocket_ping_tries = self.config.get(
            CONF_WEBSOCKET_PING_TRIES, DEFAULT_WEBSOCKET_PING_TRIES
        )
        self.websocket_ping_interval = self.config.get(
            CONF_WEBSOCKET_PING_INTERVAL, DEFAULT_WEBSOCKET_PING_INTERVAL
        )
        self.websocket_ping_timeout = self.config.get(
            CONF_WEBSOCKET_PING_TIMEOUT, DEFAULT_WEBSOCKET_PING_TIMEOUT
        )

        self.subprotocol = self.config.get(CONF_SUBPROTOCOL, DEFAULT_SUBPROTOCOL)
        self._server = None
        self.charge_points = {}
        if self.config.get(CONF_SSL, DEFAULT_SSL):
            self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # see https://community.home-assistant.io/t/certificate-authority-and-self-signed-certificate-for-ssl-tls/196970
            localhost_certfile = entry.data.get(
                CONF_SSL_CERTFILE_PATH, DEFAULT_SSL_CERTFILE_PATH
            )
            localhost_keyfile = entry.data.get(
                CONF_SSL_KEYFILE_PATH, DEFAULT_SSL_KEYFILE_PATH
            )
            self.ssl_context.load_cert_chain(
                localhost_certfile, keyfile=localhost_keyfile
            )
        else:
            self.ssl_context = None

    @staticmethod
    async def create(hass: HomeAssistant, entry: ConfigEntry):
        """Create instance and start listening for OCPP connections on given port."""
        self = CentralSystem(hass, entry)

        server = await websockets.server.serve(
            self.on_connect,
            self.host,
            self.port,
            subprotocols=[self.subprotocol],
            ping_interval=None,  # ping interval is not used here, because we send pings mamually in ChargePoint.monitor_connection()
            ping_timeout=None,
            close_timeout=self.websocket_close_timeout,
            ssl=self.ssl_context,
        )
        self._server = server
        return self

    async def on_connect(
        self, websocket: websockets.server.WebSocketServerProtocol, path: str
    ):
        """Request handler executed for every new OCPP connection."""
        if self.config.get(CONF_SKIP_SCHEMA_VALIDATION, DEFAULT_SKIP_SCHEMA_VALIDATION):
            _LOGGER.warning("Skipping websocket subprotocol validation")
        else:
            if websocket.subprotocol is not None:
                _LOGGER.info("Websocket Subprotocol matched: %s", websocket.subprotocol)
            else:
                # In the websockets lib if no subprotocols are supported by the
                # client and the server, it proceeds without a subprotocol,
                # so we have to manually close the connection.
                _LOGGER.warning(
                    "Protocols mismatched | expected Subprotocols: %s,"
                    " but client supports  %s | Closing connection",
                    websocket.available_subprotocols,
                    websocket.request_headers.get("Sec-WebSocket-Protocol", ""),
                )
                return await websocket.close()

        _LOGGER.info(f"Charger websocket path={path}")
        cp_id = path.strip("/")
        cp_id = cp_id[cp_id.rfind("/") + 1 :]

        if cp_id not in self.charge_points:
            if cp_id in self.hass.domain[DOMAIN][DEVICE_TYPE_CHARGE_POINT]:
                _LOGGER.info(
                    f"Known charge point {cp_id} connected to {self.host}:{self.port}."
                )
                charge_point = self.hass.domain[DOMAIN][DEVICE_TYPE_CHARGE_POINT]
                self.charge_points[self.cp_id] = charge_point
                await charge_point.start(websocket)
            else:
                _LOGGER.info(
                    f"Unknown charge point {cp_id} connected to {self.host}:{self.port}."
                )
        else:
            _LOGGER.info(f"Charger {cp_id} reconnected to {self.host}:{self.port}.")
            charge_point: ChargePoint = self.charge_points[self.cp_id]
            await charge_point.reconnect(websocket)
        _LOGGER.info(f"Charger {cp_id} disconnected from {self.host}:{self.port}.")

    def get_metric(self, cp_id: str, measurand: str):
        """Return last known value for given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].value
        return None

    def del_metric(self, cp_id: str, measurand: str):
        """Set given measurand to None."""
        if cp_id in self.charge_points:
            self.charge_points[cp_id]._metrics[measurand].value = None
        return None

    def get_unit(self, cp_id: str, measurand: str):
        """Return unit of given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].unit
        return None

    def get_ha_unit(self, cp_id: str, measurand: str):
        """Return home assistant unit of given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].ha_unit
        return None

    def get_extra_attr(self, cp_id: str, measurand: str):
        """Return last known extra attributes for given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].extra_attr
        return None

    def get_available(self, cp_id: str):
        """Return whether the charger is available."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].status == STATE_OK
        return False

    def get_supported_features(self, cp_id: str):
        """Return what profiles the charger supports."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].supported_features
        return 0

    async def set_max_charge_rate_amps(self, cp_id: str, value: float):
        """Set the maximum charge rate in amps."""
        if cp_id in self.charge_points:
            return await self.charge_points[cp_id].set_charge_rate(limit_amps=value)
        return False

    async def set_charger_state(
        self, cp_id: str, service_name: str, state: bool = True
    ):
        """Carry out requested service/state change on connected charger."""
        resp = False
        if cp_id in self.charge_points:
            if service_name == csvcs.service_availability.name:
                resp = await self.charge_points[cp_id].set_availability(state)
            if service_name == csvcs.service_charge_start.name:
                resp = await self.charge_points[cp_id].start_transaction()
            if service_name == csvcs.service_charge_stop.name:
                resp = await self.charge_points[cp_id].stop_transaction()
            if service_name == csvcs.service_reset.name:
                resp = await self.charge_points[cp_id].reset()
            if service_name == csvcs.service_unlock.name:
                resp = await self.charge_points[cp_id].unlock()
        return resp

    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, f"{DEVICE_TYPE_CENTRAL_SYSTEM}.{self.cs_id}")},
        }


class ChargePoint(cp):
    """Server side representation of a charger."""

    def __init__(
        self,
        cp_id: str,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ):
        """Instantiate a ChargePoint."""

        super().__init__(cp_id, None)

        self.hass = hass
        self.entry = entry
        self.config = entry.options

        for action in self.route_map:
            self.route_map[action]["_skip_schema_validation"] = self.config.get(
                CONF_SKIP_SCHEMA_VALIDATION, DEFAULT_SKIP_SCHEMA_VALIDATION
            )

        self.interval_meter_metrics = self.config.get(
            CONF_METER_INTERVAL, DEFAULT_METER_INTERVAL
        )
        self.status = "init"
        # Indicates if the charger requires a reboot to apply new
        # configuration.
        self._requires_reboot = False
        self.preparing = asyncio.Event()
        self.active_transaction_id: int = 0
        self.triggered_boot_notification = False
        self.received_boot_notification = False
        self.post_connect_success = False
        self.tasks = None
        self._charger_reports_session_energy = False
        self._metrics = defaultdict(lambda: Metric(None, None))
        self._metrics[cdet.identifier.value].value = cp_id
        self._metrics[csess.session_time.value].unit = TIME_MINUTES
        self._metrics[csess.session_energy.value].unit = UnitOfMeasure.kwh.value
        self._metrics[csess.meter_start.value].unit = UnitOfMeasure.kwh.value
        self._attr_supported_features = prof.NONE
        self._metrics[cstat.reconnects.value].value: int = 0

    async def update(self):
        """Update entities in HA."""
        er = entity_registry.async_get(self.hass)
        dr = device_registry.async_get(self.hass)
        identifiers = {(DOMAIN, f"{DEVICE_TYPE_CHARGE_POINT}.{self.cp_id}")}
        dev = dr.async_get_device(identifiers)
        # _LOGGER.info("Device id: %s updating", dev.name)
        for ent in entity_registry.async_entries_for_device(er, dev.id):
            # _LOGGER.info("Entity id: %s updating", ent.entity_id)
            self.hass.async_create_task(
                entity_component.async_update_entity(self.hass, ent.entity_id)
            )

    async def post_connect(self):
        """Logic to be executed right after a charger connects."""

        # Define custom service handles for charge point
        async def handle_clear_profile(call):
            """Handle the clear profile service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            await self.clear_profile()

        async def handle_update_firmware(call):
            """Handle the firmware update service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            url = call.data.get("firmware_url")
            delay = int(call.data.get("delay_hours", 0))
            await self.update_firmware(url, delay)

        async def handle_configure(call):
            """Handle the configure service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            key = call.data.get("ocpp_key")
            value = call.data.get("value")
            await self.configure(key, value)

        async def handle_get_configuration(call):
            """Handle the get configuration service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            key = call.data.get("ocpp_key")
            await self.get_configuration(key)

        async def handle_get_diagnostics(call):
            """Handle the get get diagnostics service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            url = call.data.get("upload_url")
            await self.get_diagnostics(url)

        async def handle_data_transfer(call):
            """Handle the data transfer service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            vendor = call.data.get("vendor_id")
            message = call.data.get("message_id", "")
            data = call.data.get("data", "")
            await self.data_transfer(vendor, message, data)

        async def handle_set_charge_rate(call):
            """Handle the data transfer service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            amps = call.data.get("limit_amps", None)
            watts = call.data.get("limit_watts", None)
            id = call.data.get("conn_id", 0)
            custom_profile = call.data.get("custom_profile", None)
            if custom_profile is not None:
                if type(custom_profile) is str:
                    custom_profile = custom_profile.replace("'", '"')
                    custom_profile = json.loads(custom_profile)
                await self.set_charge_rate(profile=custom_profile, conn_id=id)
            elif watts is not None:
                await self.set_charge_rate(limit_watts=watts, conn_id=id)
            elif amps is not None:
                await self.set_charge_rate(limit_amps=amps, conn_id=id)

        try:
            self.status = STATE_OK
            await asyncio.sleep(2)
            await self.get_supported_features()
            resp = await self.get_configuration(ckey.number_of_connectors.value)
            self._metrics[cdet.connectors.value].value = resp
            await self.get_configuration(ckey.heartbeat_interval.value)

            all_measurands = self.config.get(
                CONF_MONITORED_VARIABLES, DEFAULT_MEASURAND
            )

            accepted_measurands = []
            key = ckey.meter_values_sampled_data.value

            for measurand in all_measurands.split(","):
                _LOGGER.debug(f"'{self.id}' trying measurand '{measurand}'")
                req = call.ChangeConfigurationPayload(key=key, value=measurand)
                resp = await self.call(req)
                if resp.status == ConfigurationStatus.accepted:
                    _LOGGER.debug(f"'{self.id}' adding measurand '{measurand}'")
                    accepted_measurands.append(measurand)

            accepted_measurands = ",".join(accepted_measurands)

            _LOGGER.debug(f"'{self.id}' allowed measurands '{accepted_measurands}'")

            await self.configure(
                ckey.meter_values_sampled_data.value,
                accepted_measurands,
            )

            await self.configure(
                ckey.meter_value_sample_interval.value,
                str(self.config.get(CONF_METER_INTERVAL, DEFAULT_METER_INTERVAL)),
            )
            await self.configure(
                ckey.clock_aligned_data_interval.value,
                str(self.config.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)),
            )
            #            await self.configure(
            #                "StopTxnSampledData", ",".join(self.config[CONF_MONITORED_VARIABLES])
            #            )
            #            await self.start_transaction()

            # Register custom services with home assistant
            self.hass.services.async_register(
                DOMAIN,
                csvcs.service_configure.value,
                handle_configure,
                CONF_SERVICE_DATA_SCHEMA,
            )
            self.hass.services.async_register(
                DOMAIN,
                csvcs.service_get_configuration.value,
                handle_get_configuration,
                GCONF_SERVICE_DATA_SCHEMA,
            )
            self.hass.services.async_register(
                DOMAIN,
                csvcs.service_data_transfer.value,
                handle_data_transfer,
                TRANS_SERVICE_DATA_SCHEMA,
            )
            if prof.SMART in self._attr_supported_features:
                self.hass.services.async_register(
                    DOMAIN, csvcs.service_clear_profile.value, handle_clear_profile
                )
                self.hass.services.async_register(
                    DOMAIN,
                    csvcs.service_set_charge_rate.value,
                    handle_set_charge_rate,
                    CHRGR_SERVICE_DATA_SCHEMA,
                )
            if prof.FW in self._attr_supported_features:
                self.hass.services.async_register(
                    DOMAIN,
                    csvcs.service_update_firmware.value,
                    handle_update_firmware,
                    UFW_SERVICE_DATA_SCHEMA,
                )
                self.hass.services.async_register(
                    DOMAIN,
                    csvcs.service_get_diagnostics.value,
                    handle_get_diagnostics,
                    GDIAG_SERVICE_DATA_SCHEMA,
                )
            self.post_connect_success = True
            _LOGGER.debug(f"'{self.id}' post connection setup completed successfully")

            # nice to have, but not needed for integration to function
            # and can cause issues with some chargers
            await self.configure(ckey.web_socket_ping_interval.value, "60")
            await self.set_availability()
            if prof.REM in self._attr_supported_features:
                if self.received_boot_notification is False:
                    await self.trigger_boot_notification()
                await self.trigger_status_notification()
        except NotImplementedError as e:
            _LOGGER.error("Configuration of the charger failed: %s", e)

    async def get_supported_features(self):
        """Get supported features."""
        req = call.GetConfigurationPayload(key=[ckey.supported_feature_profiles.value])
        resp = await self.call(req)
        feature_list = (resp.configuration_key[0][om.value.value]).split(",")
        if feature_list[0] == "":
            _LOGGER.warning("No feature profiles detected, defaulting to Core")
            await self.notify_ha("No feature profiles detected, defaulting to Core")
            feature_list = [om.feature_profile_core.value]
        if self.config.get(CONF_FORCE_SMART_CHARGING, DEFAULT_FORCE_SMART_CHARGING):
            _LOGGER.warning("Force Smart Charging feature profile")
            self._attr_supported_features |= prof.SMART
        for item in feature_list:
            item = item.strip().replace(" ", "")
            if item == om.feature_profile_core.value:
                self._attr_supported_features |= prof.CORE
            elif item == om.feature_profile_firmware.value:
                self._attr_supported_features |= prof.FW
            elif item == om.feature_profile_smart.value:
                self._attr_supported_features |= prof.SMART
            elif item == om.feature_profile_reservation.value:
                self._attr_supported_features |= prof.RES
            elif item == om.feature_profile_remote.value:
                self._attr_supported_features |= prof.REM
            elif item == om.feature_profile_auth.value:
                self._attr_supported_features |= prof.AUTH
            else:
                _LOGGER.warning("Unknown feature profile detected ignoring: %s", item)
                await self.notify_ha(
                    f"Warning: Unknown feature profile detected ignoring {item}"
                )
        self._metrics[cdet.features.value].value = self._attr_supported_features
        _LOGGER.debug("Feature profiles returned: %s", self._attr_supported_features)

    async def trigger_boot_notification(self):
        """Trigger a boot notification."""
        req = call.TriggerMessagePayload(
            requested_message=MessageTrigger.boot_notification
        )
        resp = await self.call(req)
        if resp.status == TriggerMessageStatus.accepted:
            self.triggered_boot_notification = True
            return True
        else:
            self.triggered_boot_notification = False
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def trigger_status_notification(self):
        """Trigger status notifications for all connectors."""
        return_value = True
        nof_connectors = int(self._metrics[cdet.connectors.value].value)
        for id in range(0, nof_connectors + 1):
            _LOGGER.debug(f"trigger status notification for connector={id}")
            req = call.TriggerMessagePayload(
                requested_message=MessageTrigger.status_notification,
                connector_id=int(id),
            )
            resp = await self.call(req)
            if resp.status != TriggerMessageStatus.accepted:
                _LOGGER.warning("Failed with response: %s", resp.status)
                return_value = False
        return return_value

    async def clear_profile(self):
        """Clear all charging profiles."""
        req = call.ClearChargingProfilePayload()
        resp = await self.call(req)
        if resp.status == ClearChargingProfileStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Clear profile failed with response {resp.status}"
            )
            return False

    async def set_charge_rate(
        self,
        limit_amps: int = 32,
        limit_watts: int = 22000,
        conn_id: int = 0,
        profile: dict | None = None,
    ):
        """Set a charging profile with defined limit."""
        if profile is not None:  # assumes advanced user and correct profile format
            req = call.SetChargingProfilePayload(
                connector_id=conn_id, cs_charging_profiles=profile
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                return True
            else:
                _LOGGER.warning("Failed with response: %s", resp.status)
                await self.notify_ha(
                    f"Warning: Set charging profile failed with response {resp.status}"
                )
                return False

        if prof.SMART in self._attr_supported_features:
            resp = await self.get_configuration(
                ckey.charging_schedule_allowed_charging_rate_unit.value
            )
            _LOGGER.info(
                "Charger supports setting the following units: %s",
                resp,
            )
            _LOGGER.info("If more than one unit supported default unit is Amps")
            if om.current.value in resp:
                lim = limit_amps
                units = ChargingRateUnitType.amps.value
            else:
                lim = limit_watts
                units = ChargingRateUnitType.watts.value
            resp = await self.get_configuration(
                ckey.charge_profile_max_stack_level.value
            )
            stack_level = int(resp)
            req = call.SetChargingProfilePayload(
                connector_id=conn_id,
                cs_charging_profiles={
                    om.charging_profile_id.value: 8,
                    om.stack_level.value: stack_level,
                    om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                    om.charging_profile_purpose.value: ChargingProfilePurposeType.charge_point_max_profile.value,
                    om.charging_schedule.value: {
                        om.charging_rate_unit.value: units,
                        om.charging_schedule_period.value: [
                            {om.start_period.value: 0, om.limit.value: lim}
                        ],
                    },
                },
            )
        else:
            _LOGGER.info("Smart charging is not supported by this charger")
            return False
        resp = await self.call(req)
        if resp.status == ChargingProfileStatus.accepted:
            return True
        else:
            _LOGGER.debug(
                "ChargePointMaxProfile is not supported by this charger, trying TxDefaultProfile instead..."
            )
            # try a lower stack level for chargers where level < maximum, not <=
            req = call.SetChargingProfilePayload(
                connector_id=conn_id,
                cs_charging_profiles={
                    om.charging_profile_id.value: 8,
                    om.stack_level.value: stack_level - 1,
                    om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                    om.charging_profile_purpose.value: ChargingProfilePurposeType.tx_default_profile.value,
                    om.charging_schedule.value: {
                        om.charging_rate_unit.value: units,
                        om.charging_schedule_period.value: [
                            {om.start_period.value: 0, om.limit.value: lim}
                        ],
                    },
                },
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                return True
            else:
                _LOGGER.warning("Failed with response: %s", resp.status)
                await self.notify_ha(
                    f"Warning: Set charging profile failed with response {resp.status}"
                )
                return False

    async def set_availability(self, state: bool = True):
        """Change availability."""
        if state is True:
            typ = AvailabilityType.operative.value
        else:
            typ = AvailabilityType.inoperative.value

        req = call.ChangeAvailabilityPayload(connector_id=0, type=typ)
        resp = await self.call(req)
        if resp.status == AvailabilityStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Set availability failed with response {resp.status}"
            )
            return False

    async def start_transaction(self):
        """Remote start a transaction.

        Check if authorisation enabled, if it is disable it before remote start
        """
        resp = await self.get_configuration(ckey.authorize_remote_tx_requests.value)
        if resp is True:
            await self.configure(ckey.authorize_remote_tx_requests.value, "false")
        req = call.RemoteStartTransactionPayload(
            connector_id=1, id_tag=self._metrics[cdet.identifier.value].value[:20]
        )
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Start transaction failed with response {resp.status}"
            )
            return False

    async def stop_transaction(self):
        """Request remote stop of current transaction.

        Leaves charger in finishing state until unplugged.
        Use reset() to make the charger available again for remote start
        """
        if self.active_transaction_id == 0:
            return True
        req = call.RemoteStopTransactionPayload(
            transaction_id=self.active_transaction_id
        )
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Stop transaction failed with response {resp.status}"
            )
            return False

    async def reset(self, typ: str = ResetType.hard):
        """Hard reset charger unless soft reset requested."""
        self._metrics[cstat.reconnects.value].value = 0
        req = call.ResetPayload(typ)
        resp = await self.call(req)
        if resp.status == ResetStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(f"Warning: Reset failed with response {resp.status}")
            return False

    async def unlock(self, connector_id: int = 1):
        """Unlock charger if requested."""
        req = call.UnlockConnectorPayload(connector_id)
        resp = await self.call(req)
        if resp.status == UnlockStatus.unlocked:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(f"Warning: Unlock failed with response {resp.status}")
            return False

    async def update_firmware(self, firmware_url: str, wait_time: int = 0):
        """Update charger with new firmware if available."""
        """where firmware_url is the http or https url of the new firmware"""
        """and wait_time is hours from now to wait before install"""
        if prof.FW in self._attr_supported_features:
            schema = vol.Schema(vol.Url())
            try:
                url = schema(firmware_url)
            except vol.MultipleInvalid as e:
                _LOGGER.debug("Failed to parse url: %s", e)
            update_time = (
                datetime.now(tz=timezone.utc) + timedelta(hours=wait_time)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            req = call.UpdateFirmwarePayload(location=url, retrieve_date=update_time)
            resp = await self.call(req)
            _LOGGER.info("Response: %s", resp)
            return True
        else:
            _LOGGER.warning("Charger does not support ocpp firmware updating")
            return False

    async def get_diagnostics(self, upload_url: str):
        """Upload diagnostic data to server from charger."""
        if prof.FW in self._attr_supported_features:
            schema = vol.Schema(vol.Url())
            try:
                url = schema(upload_url)
            except vol.MultipleInvalid as e:
                _LOGGER.warning("Failed to parse url: %s", e)
            req = call.GetDiagnosticsPayload(location=url)
            resp = await self.call(req)
            _LOGGER.info("Response: %s", resp)
            return True
        else:
            _LOGGER.warning("Charger does not support ocpp diagnostics uploading")
            return False

    async def data_transfer(self, vendor_id: str, message_id: str = "", data: str = ""):
        """Request vendor specific data transfer from charger."""
        req = call.DataTransferPayload(
            vendor_id=vendor_id, message_id=message_id, data=data
        )
        resp = await self.call(req)
        if resp.status == DataTransferStatus.accepted:
            _LOGGER.info(
                "Data transfer [vendorId(%s), messageId(%s), data(%s)] response: %s",
                vendor_id,
                message_id,
                data,
                resp.data,
            )
            self._metrics[cdet.data_response.value].value = datetime.now(
                tz=timezone.utc
            )
            self._metrics[cdet.data_response.value].extra_attr = {message_id: resp.data}
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Data transfer failed with response {resp.status}"
            )
            return False

    async def get_configuration(self, key: str = ""):
        """Get Configuration of charger for supported keys else return None."""
        if key == "":
            req = call.GetConfigurationPayload()
        else:
            req = call.GetConfigurationPayload(key=[key])
        resp = await self.call(req)
        if resp.configuration_key is not None:
            value = resp.configuration_key[0][om.value.value]
            _LOGGER.debug("Get Configuration for %s: %s", key, value)
            self._metrics[cdet.config_response.value].value = datetime.now(
                tz=timezone.utc
            )
            self._metrics[cdet.config_response.value].extra_attr = {key: value}
            return value
        if resp.unknown_key is not None:
            _LOGGER.warning("Get Configuration returned unknown key for: %s", key)
            await self.notify_ha(f"Warning: charger reports {key} is unknown")
            return None

    async def configure(self, key: str, value: str):
        """Configure charger by setting the key to target value.

        First the configuration key is read using GetConfiguration. The key's
        value is compared with the target value. If the key is already set to
        the correct value nothing is done.

        If the key has a different value a ChangeConfiguration request is issued.

        """
        req = call.GetConfigurationPayload(key=[key])

        resp = await self.call(req)

        if resp.unknown_key is not None:
            if key in resp.unknown_key:
                _LOGGER.warning("%s is unknown (not supported)", key)
                return

        for key_value in resp.configuration_key:
            # If the key already has the targeted value we don't need to set
            # it.
            if key_value[om.key.value] == key and key_value[om.value.value] == value:
                return

            if key_value.get(om.readonly.name, False):
                _LOGGER.warning("%s is a read only setting", key)
                await self.notify_ha(f"Warning: {key} is read-only")

        req = call.ChangeConfigurationPayload(key=key, value=value)

        resp = await self.call(req)

        if resp.status in [
            ConfigurationStatus.rejected,
            ConfigurationStatus.not_supported,
        ]:
            _LOGGER.warning("%s while setting %s to %s", resp.status, key, value)
            await self.notify_ha(
                f"Warning: charger reported {resp.status} while setting {key}={value}"
            )

        if resp.status == ConfigurationStatus.reboot_required:
            self._requires_reboot = True
            await self.notify_ha(f"A reboot is required to apply {key}={value}")

    async def _get_specific_response(self, unique_id, timeout):
        # The ocpp library silences CallErrors by default. See
        # https://github.com/mobilityhouse/ocpp/issues/104.
        # This code 'unsilences' CallErrors by raising them as exception
        # upon receiving.
        resp = await super()._get_specific_response(unique_id, timeout)

        if isinstance(resp, CallError):
            raise resp.to_exception()

        return resp

    async def monitor_connection(self):
        """Monitor the connection, by measuring the connection latency."""
        self._metrics[cstat.latency_ping.value].unit = "ms"
        self._metrics[cstat.latency_pong.value].unit = "ms"
        connection = self._connection
        timeout_counter = 0

        websocket_ping_tries = self.config.get(
            CONF_WEBSOCKET_PING_TRIES, DEFAULT_WEBSOCKET_PING_TRIES
        )

        while connection.open:
            try:
                websocket_ping_interval = self.config.get(
                    CONF_WEBSOCKET_PING_INTERVAL, DEFAULT_WEBSOCKET_PING_INTERVAL
                )
                websocket_ping_timeout = self.config.get(
                    CONF_WEBSOCKET_PING_TIMEOUT, DEFAULT_WEBSOCKET_PING_TIMEOUT
                )
                await asyncio.sleep(websocket_ping_interval)
                time0 = time.perf_counter()
                latency_ping = websocket_ping_timeout * 1000
                pong_waiter = await asyncio.wait_for(
                    connection.ping(), timeout=websocket_ping_timeout
                )
                time1 = time.perf_counter()
                latency_ping = round(time1 - time0, 3) * 1000
                latency_pong = websocket_ping_timeout * 1000
                await asyncio.wait_for(pong_waiter, timeout=websocket_ping_timeout)
                timeout_counter = 0
                time2 = time.perf_counter()
                latency_pong = round(time2 - time1, 3) * 1000
                _LOGGER.debug(
                    f"Connection latency from '{self.cs_id}' to '{self.cp_id}': ping={latency_ping} ms, pong={latency_pong} ms",
                )
                self._metrics[cstat.latency_ping.value].value = latency_ping
                self._metrics[cstat.latency_pong.value].value = latency_pong

            except asyncio.TimeoutError as timeout_exception:
                _LOGGER.debug(
                    f"Connection latency from '{self.cs_id}' to '{self.cp_id}': ping={latency_ping} ms, pong={latency_pong} ms",
                )
                self._metrics[cstat.latency_ping.value].value = latency_ping
                self._metrics[cstat.latency_pong.value].value = latency_pong
                timeout_counter += 1
                if timeout_counter > websocket_ping_tries:
                    _LOGGER.debug(
                        f"Connection to '{self.cp_id}' timed out after '{websocket_ping_tries}' ping tries",
                    )
                    raise timeout_exception
                else:
                    continue

    async def _handle_call(self, msg):
        try:
            await super()._handle_call(msg)
        except NotImplementedError as e:
            response = msg.create_call_error(e).to_json()
            await self._send(response)

    async def start(self, connection: websockets.server.WebSocketServerProtocol):
        """Start charge point."""
        self._connection = connection
        await self.run(
            [super().start(), self.post_connect(), self.monitor_connection()]
        )

    async def run(self, tasks):
        """Run a specified list of tasks."""
        self.tasks = [asyncio.ensure_future(task) for task in tasks]
        try:
            await asyncio.gather(*self.tasks)
        except asyncio.TimeoutError:
            pass
        except websockets.exceptions.WebSocketException as websocket_exception:
            _LOGGER.debug(f"Connection closed to '{self.id}': {websocket_exception}")
        except Exception as other_exception:
            _LOGGER.error(
                f"Unexpected exception in connection to '{self.id}': '{other_exception}'",
                exc_info=True,
            )
        finally:
            await self.stop()

    async def stop(self):
        """Close connection and cancel ongoing tasks."""
        self.status = STATE_UNAVAILABLE
        if self._connection.open:
            _LOGGER.debug(f"Closing websocket to '{self.id}'")
            await self._connection.close()
        for task in self.tasks:
            task.cancel()

    async def reconnect(self, connection: websockets.server.WebSocketServerProtocol):
        """Reconnect charge point."""
        _LOGGER.debug(f"Reconnect websocket to {self.id}")

        await self.stop()
        self.status = STATE_OK
        self._connection = connection
        self._metrics[cstat.reconnects.value].value += 1
        if self.post_connect_success is True:
            await self.run([super().start(), self.monitor_connection()])
        else:
            await self.run(
                [super().start(), self.post_connect(), self.monitor_connection()]
            )

    async def async_update_device_info(self, boot_info: dict):
        """Update device info asynchronuously."""

        _LOGGER.debug("Updating device info %s: %s", self.cp_id, boot_info)
        identifiers = {
            (DOMAIN, f"{DEVICE_TYPE_CHARGE_POINT}.{self.cp_id}"),
        }
        serial = boot_info.get(om.charge_point_serial_number.name, None)
        if serial is not None:
            identifiers.add((DOMAIN, f"{DEVICE_TYPE_CHARGE_POINT}.{serial}"))

        registry = device_registry.async_get(self.hass)
        registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=identifiers,
            name=self.cp_id,
            manufacturer=boot_info.get(om.charge_point_vendor.name, None),
            model=boot_info.get(om.charge_point_model.name, None),
            suggested_area="Garage",
            sw_version=boot_info.get(om.firmware_version.name, None),
        )

    def process_phases(self, data):
        """Process phase data from meter values payload."""

        def average_of_nonzero(values):
            nonzero_values: list = [v for v in values if float(v) != 0.0]
            nof_values: int = len(nonzero_values)
            average = sum(nonzero_values) / nof_values if nof_values > 0 else 0
            return average

        measurand_data = {}
        for item in data:
            # create ordered Dict for each measurand, eg {"voltage":{"unit":"V","L1-N":"230"...}}
            measurand = item.get(om.measurand.value, None)
            phase = item.get(om.phase.value, None)
            value = item.get(om.value.value, None)
            unit = item.get(om.unit.value, None)
            context = item.get(om.context.value, None)
            if measurand is not None and phase is not None and unit is not None:
                if measurand not in measurand_data:
                    measurand_data[measurand] = {}
                measurand_data[measurand][om.unit.value] = unit
                measurand_data[measurand][phase] = float(value)
                self._metrics[measurand].unit = unit
                self._metrics[measurand].extra_attr[om.unit.value] = unit
                self._metrics[measurand].extra_attr[phase] = float(value)
                self._metrics[measurand].extra_attr[om.context.value] = context

        line_phases = [Phase.l1.value, Phase.l2.value, Phase.l3.value]
        line_to_neutral_phases = [Phase.l1_n.value, Phase.l2_n.value, Phase.l3_n.value]
        line_to_line_phases = [Phase.l1_l2.value, Phase.l2_l3.value, Phase.l3_l1.value]

        for metric, phase_info in measurand_data.items():
            metric_value = None
            if metric in [Measurand.voltage.value]:
                if not phase_info.keys().isdisjoint(line_to_neutral_phases):
                    # Line to neutral voltages are averaged
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0) for phase in line_to_neutral_phases]
                    )
                elif not phase_info.keys().isdisjoint(line_to_line_phases):
                    # Line to line voltages are averaged and converted to line to neutral
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0) for phase in line_to_line_phases]
                    ) / sqrt(3)
                elif not phase_info.keys().isdisjoint(line_phases):
                    # Workaround for chargers that don't follow engineering convention
                    # Assumes voltages are line to neutral
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0) for phase in line_phases]
                    )
            else:
                if not phase_info.keys().isdisjoint(line_phases):
                    metric_value = sum(
                        phase_info.get(phase, 0) for phase in line_phases
                    )
                elif not phase_info.keys().isdisjoint(line_to_neutral_phases):
                    # Workaround for some chargers that erroneously use line to neutral for current
                    metric_value = sum(
                        phase_info.get(phase, 0) for phase in line_to_neutral_phases
                    )

            if metric_value is not None:
                metric_unit = phase_info.get(om.unit.value)
                _LOGGER.debug(
                    "process_phases: metric: %s, phase_info: %s value: %f unit :%s",
                    metric,
                    phase_info,
                    metric_value,
                    metric_unit,
                )
                if metric_unit == DEFAULT_POWER_UNIT:
                    self._metrics[metric].value = float(metric_value) / 1000
                    self._metrics[metric].unit = HA_POWER_UNIT
                elif metric_unit == DEFAULT_ENERGY_UNIT:
                    self._metrics[metric].value = float(metric_value) / 1000
                    self._metrics[metric].unit = HA_ENERGY_UNIT
                else:
                    self._metrics[metric].value = float(metric_value)
                    self._metrics[metric].unit = metric_unit

    @on(Action.MeterValues)
    def on_meter_values(
        self, connector_id: int, meter_value: dict, **kwargs
    ):  # noqa: C901
        """Request handler for MeterValues Calls."""

        transaction_id: int = kwargs.get(om.transaction_id.name, 0)

        # If missing meter_start or active_transaction_id try to restore from HA states. If HA
        # does not have values either, generate new ones.
        if self._metrics[csess.meter_start.value].value is None:
            value = self.get_ha_metric(csess.meter_start.value)
            if value is None:
                value = self._metrics[DEFAULT_MEASURAND].value
            else:
                value = float(value)
                _LOGGER.debug(
                    f"{csess.meter_start.value} was None, restored value={value} from HA."
                )
            self._metrics[csess.meter_start.value].value = value
        if self._metrics[csess.transaction_id.value].value is None:
            value = self.get_ha_metric(csess.transaction_id.value)
            if value is None:
                value = kwargs.get(om.transaction_id.name)
            else:
                value = int(value)
                _LOGGER.debug(
                    f"{csess.transaction_id.value} was None, restored value={value} from HA."
                )
            self._metrics[csess.transaction_id.value].value = value
            self.active_transaction_id = value

        transaction_matches: bool = False
        # match is also false if no transaction is in progress ie active_transaction_id==transaction_id==0
        if transaction_id == self.active_transaction_id and transaction_id != 0:
            transaction_matches = True
        elif transaction_id != 0:
            _LOGGER.warning("Unknown transaction detected with id=%i", transaction_id)

        for bucket in meter_value:
            unprocessed = bucket[om.sampled_value.name]
            processed_keys = []
            for idx, sampled_value in enumerate(bucket[om.sampled_value.name]):
                measurand = sampled_value.get(om.measurand.value, None)
                value = sampled_value.get(om.value.value, None)
                unit = sampled_value.get(om.unit.value, None)
                phase = sampled_value.get(om.phase.value, None)
                location = sampled_value.get(om.location.value, None)
                context = sampled_value.get(om.context.value, None)

                if len(sampled_value.keys()) == 1:  # Backwards compatibility
                    measurand = DEFAULT_MEASURAND
                    unit = DEFAULT_ENERGY_UNIT

                if measurand == DEFAULT_MEASURAND and unit is None:
                    unit = DEFAULT_ENERGY_UNIT

                if self._metrics[csess.meter_start.value].value == 0:
                    # Charger reports Energy.Active.Import.Register directly as Session energy for transactions.
                    self._charger_reports_session_energy = True

                if phase is None:
                    if unit == DEFAULT_POWER_UNIT:
                        self._metrics[measurand].value = float(value) / 1000
                        self._metrics[measurand].unit = HA_POWER_UNIT
                    elif (
                        measurand == DEFAULT_MEASURAND
                        and self._charger_reports_session_energy
                    ):
                        if transaction_matches:
                            if unit == DEFAULT_ENERGY_UNIT:
                                value = float(value) / 1000
                                unit = HA_ENERGY_UNIT
                            self._metrics[csess.session_energy.value].value = float(
                                value
                            )
                            self._metrics[csess.session_energy.value].unit = unit
                            self._metrics[csess.session_energy.value].extra_attr[
                                cstat.id_tag.name
                            ] = self._metrics[cstat.id_tag.value].value
                        else:
                            if unit == DEFAULT_ENERGY_UNIT:
                                value = float(value) / 1000
                                unit = HA_ENERGY_UNIT
                            self._metrics[measurand].value = float(value)
                            self._metrics[measurand].unit = unit
                    elif unit == DEFAULT_ENERGY_UNIT:
                        if transaction_matches:
                            self._metrics[measurand].value = float(value) / 1000
                            self._metrics[measurand].unit = HA_ENERGY_UNIT
                    else:
                        self._metrics[measurand].value = float(value)
                        self._metrics[measurand].unit = unit
                    if location is not None:
                        self._metrics[measurand].extra_attr[
                            om.location.value
                        ] = location
                    if context is not None:
                        self._metrics[measurand].extra_attr[om.context.value] = context
                    processed_keys.append(idx)
            for idx in sorted(processed_keys, reverse=True):
                unprocessed.pop(idx)
            # _LOGGER.debug("Meter data not yet processed: %s", unprocessed)
            if unprocessed is not None:
                self.process_phases(unprocessed)
        if transaction_matches:
            self._metrics[csess.session_time.value].value = round(
                (
                    int(time.time())
                    - float(self._metrics[csess.transaction_id.value].value)
                )
                / 60
            )
            self._metrics[csess.session_time.value].unit = "min"
            if (
                self._metrics[csess.meter_start.value].value is not None
                and not self._charger_reports_session_energy
            ):
                self._metrics[csess.session_energy.value].value = float(
                    self._metrics[DEFAULT_MEASURAND].value or 0
                ) - float(self._metrics[csess.meter_start.value].value)
                self._metrics[csess.session_energy.value].extra_attr[
                    cstat.id_tag.name
                ] = self._metrics[cstat.id_tag.value].value
        self.hass.async_create_task(self.update())
        return call_result.MeterValuesPayload()

    @on(Action.BootNotification)
    def on_boot_notification(self, **kwargs):
        """Handle a boot notification."""
        resp = call_result.BootNotificationPayload(
            current_time=datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            interval=3600,
            status=RegistrationStatus.accepted.value,
        )
        self.received_boot_notification = True
        _LOGGER.debug("Received boot notification for %s: %s", self.id, kwargs)
        # update metrics
        self._metrics[cdet.model.value].value = kwargs.get(
            om.charge_point_model.name, None
        )
        self._metrics[cdet.vendor.value].value = kwargs.get(
            om.charge_point_vendor.name, None
        )
        self._metrics[cdet.firmware_version.value].value = kwargs.get(
            om.firmware_version.name, None
        )
        self._metrics[cdet.serial.value].value = kwargs.get(
            om.charge_point_serial_number.name, None
        )

        self.hass.async_create_task(self.async_update_device_info(kwargs))
        self.hass.async_create_task(self.update())
        if self.triggered_boot_notification is False:
            self.hass.async_create_task(self.notify_ha(f"Charger {self.id} rebooted"))
            self.hass.async_create_task(self.post_connect())
        return resp

    @on(Action.StatusNotification)
    def on_status_notification(self, connector_id, error_code, status, **kwargs):
        """Handle a status notification."""

        if connector_id == 0 or connector_id is None:
            self._metrics[cstat.status.value].value = status
            self._metrics[cstat.error_code.value].value = error_code
        elif connector_id == 1:
            self._metrics[cstat.status_connector.value].value = status
            self._metrics[cstat.error_code_connector.value].value = error_code
        if connector_id >= 1:
            self._metrics[cstat.status_connector.value].extra_attr[
                connector_id
            ] = status
            self._metrics[cstat.error_code_connector.value].extra_attr[
                connector_id
            ] = error_code
        if (
            status == ChargePointStatus.suspended_ev.value
            or status == ChargePointStatus.suspended_evse.value
        ):
            if Measurand.current_import.value in self._metrics:
                self._metrics[Measurand.current_import.value].value = 0
            if Measurand.power_active_import.value in self._metrics:
                self._metrics[Measurand.power_active_import.value].value = 0
            if Measurand.power_reactive_import.value in self._metrics:
                self._metrics[Measurand.power_reactive_import.value].value = 0
            if Measurand.current_export.value in self._metrics:
                self._metrics[Measurand.current_export.value].value = 0
            if Measurand.power_active_export.value in self._metrics:
                self._metrics[Measurand.power_active_export.value].value = 0
            if Measurand.power_reactive_export.value in self._metrics:
                self._metrics[Measurand.power_reactive_export.value].value = 0
        self.hass.async_create_task(self.update())
        return call_result.StatusNotificationPayload()

    @on(Action.FirmwareStatusNotification)
    def on_firmware_status(self, status, **kwargs):
        """Handle firmware status notification."""
        self._metrics[cstat.firmware_status.value].value = status
        self.hass.async_create_task(self.update())
        self.hass.async_create_task(self.notify_ha(f"Firmware upload status: {status}"))
        return call_result.FirmwareStatusNotificationPayload()

    @on(Action.DiagnosticsStatusNotification)
    def on_diagnostics_status(self, status, **kwargs):
        """Handle diagnostics status notification."""
        _LOGGER.info("Diagnostics upload status: %s", status)
        self.hass.async_create_task(
            self.notify_ha(f"Diagnostics upload status: {status}")
        )
        return call_result.DiagnosticsStatusNotificationPayload()

    @on(Action.SecurityEventNotification)
    def on_security_event(self, type, timestamp, **kwargs):
        """Handle security event notification."""
        _LOGGER.info(
            "Security event notification received: %s at %s [techinfo: %s]",
            type,
            timestamp,
            kwargs.get(om.tech_info.name, "none"),
        )
        self.hass.async_create_task(
            self.notify_ha(f"Security event notification received: {type}")
        )
        return call_result.SecurityEventNotificationPayload()

    def get_authorization_status(self, id_tag):
        """Get the authorization status for an id_tag."""
        # find the tag using the id
        tag = self.hass.data[DOMAIN][DEVICE_TYPE_TAG].get(id_tag, None)
        if tag:
            return tag.config.get(CONF_AUTH_STATUS, DEFAULT_AUTH_STATUS)
        # use the default authorization status of this charge point of the tag was not found
        return self.config.get(CONF_AUTH_STATUS, DEFAULT_AUTH_STATUS)

    @on(Action.Authorize)
    def on_authorize(self, id_tag, **kwargs):
        """Handle an Authorization request."""
        self._metrics[cstat.id_tag.value].value = id_tag
        auth_status = self.get_authorization_status(id_tag)
        return call_result.AuthorizePayload(id_tag_info={om.status.value: auth_status})

    @on(Action.StartTransaction)
    def on_start_transaction(self, connector_id, id_tag, meter_start, **kwargs):
        """Handle a Start Transaction request."""

        auth_status = self.get_authorization_status(id_tag)
        if auth_status == AuthorizationStatus.accepted.value:
            self.active_transaction_id = int(time.time())
            self._metrics[cstat.id_tag.value].value = id_tag
            self._metrics[cstat.stop_reason.value].value = ""
            self._metrics[csess.transaction_id.value].value = self.active_transaction_id
            self._metrics[csess.meter_start.value].value = int(meter_start) / 1000
            result = call_result.StartTransactionPayload(
                id_tag_info={om.status.value: AuthorizationStatus.accepted.value},
                transaction_id=self.active_transaction_id,
            )
        else:
            result = call_result.StartTransactionPayload(
                id_tag_info={om.status.value: auth_status}, transaction_id=0
            )
        self.hass.async_create_task(self.update())
        return result

    @on(Action.StopTransaction)
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        """Stop the current transaction."""

        if transaction_id != self.active_transaction_id:
            _LOGGER.error(
                "Stop transaction received for unknown transaction id=%i",
                transaction_id,
            )
        self.active_transaction_id = 0
        self._metrics[cstat.stop_reason.value].value = kwargs.get(om.reason.name, None)
        if (
            self._metrics[csess.meter_start.value].value is not None
            and not self._charger_reports_session_energy
        ):
            self._metrics[csess.session_energy.value].value = int(
                meter_stop
            ) / 1000 - float(self._metrics[csess.meter_start.value].value)
        if Measurand.current_import.value in self._metrics:
            self._metrics[Measurand.current_import.value].value = 0
        if Measurand.power_active_import.value in self._metrics:
            self._metrics[Measurand.power_active_import.value].value = 0
        if Measurand.power_reactive_import.value in self._metrics:
            self._metrics[Measurand.power_reactive_import.value].value = 0
        if Measurand.current_export.value in self._metrics:
            self._metrics[Measurand.current_export.value].value = 0
        if Measurand.power_active_export.value in self._metrics:
            self._metrics[Measurand.power_active_export.value].value = 0
        if Measurand.power_reactive_export.value in self._metrics:
            self._metrics[Measurand.power_reactive_export.value].value = 0
        self.hass.async_create_task(self.update())
        return call_result.StopTransactionPayload(
            id_tag_info={om.status.value: AuthorizationStatus.accepted.value}
        )

    @on(Action.DataTransfer)
    def on_data_transfer(self, vendor_id, **kwargs):
        """Handle a Data transfer request."""
        _LOGGER.debug("Data transfer received from %s: %s", self.id, kwargs)
        self._metrics[cdet.data_transfer.value].value = datetime.now(tz=timezone.utc)
        self._metrics[cdet.data_transfer.value].extra_attr = {vendor_id: kwargs}
        return call_result.DataTransferPayload(status=DataTransferStatus.accepted.value)

    @on(Action.Heartbeat)
    def on_heartbeat(self, **kwargs):
        """Handle a Heartbeat."""
        now = datetime.now(tz=timezone.utc)
        self._metrics[cstat.heartbeat.value].value = now
        self.hass.async_create_task(self.update())
        return call_result.HeartbeatPayload(
            current_time=now.strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    @property
    def supported_features(self) -> int:
        """Flag of Ocpp features that are supported."""
        return self._attr_supported_features

    def get_metric(self, measurand: str):
        """Return last known value for given measurand."""
        return self._metrics[measurand].value

    def get_ha_metric(self, measurand: str):
        """Return last known value in HA for given measurand."""
        entity_id = "sensor." + "_".join(
            [self.cp_id.lower(), measurand.lower().replace(".", "_")]
        )
        try:
            value = self.hass.states.get(entity_id).state
        except Exception as e:
            _LOGGER.debug(f"An error occurred when getting entity state from HA: {e}")
            return None
        if value == STATE_UNAVAILABLE or value == STATE_UNKNOWN:
            return None
        return value

    def get_extra_attr(self, measurand: str):
        """Return last known extra attributes for given measurand."""
        return self._metrics[measurand].extra_attr

    def get_unit(self, measurand: str):
        """Return unit of given measurand."""
        return self._metrics[measurand].unit

    def get_ha_unit(self, measurand: str):
        """Return home assistant unit of given measurand."""
        return self._metrics[measurand].ha_unit

    async def notify_ha(self, msg: str, title: str = "Ocpp integration"):
        """Notify user via HA web frontend."""
        await self.hass.services.async_call(
            PN_DOMAIN,
            "create",
            service_data={
                "title": title,
                "message": msg,
            },
            blocking=False,
        )
        return True


class Metric:
    """Metric class."""

    def __init__(self, value, unit):
        """Initialize a Metric."""
        self._value = value
        self._unit = unit
        self._extra_attr = {}

    @property
    def value(self):
        """Get the value of the metric."""
        return self._value

    @value.setter
    def value(self, value):
        """Set the value of the metric."""
        self._value = value

    @property
    def unit(self):
        """Get the unit of the metric."""
        return self._unit

    @unit.setter
    def unit(self, unit: str):
        """Set the unit of the metric."""
        self._unit = unit

    @property
    def ha_unit(self):
        """Get the home assistant unit of the metric."""
        return UNITS_OCCP_TO_HA.get(self._unit, self._unit)

    @property
    def extra_attr(self):
        """Get the extra attributes of the metric."""
        return self._extra_attr

    @extra_attr.setter
    def extra_attr(self, extra_attr: dict):
        """Set the unit of the metric."""
        self._extra_attr = extra_attr
