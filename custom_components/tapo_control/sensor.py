"""Tapo camera sensors."""

import datetime
import os
import re

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    STATE_UNAVAILABLE,
    UnitOfInformation,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    ENABLE_MEDIA_SYNC,
    LOGGER,
    MEDIA_SYNC_COLD_STORAGE_PATH,
    MEDIA_SYNC_HOURS,
    TAPO_CARE_CLEANUP_TIME,
    RECORDINGS_SOURCE,
    RECORDINGS_SOURCE_SD,
    RECORDINGS_SOURCE_TAPO_CARE,
)
from .tapo.entities import TapoSensorEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Setup Tapo camera sensors using config entry."""
    LOGGER.debug("Setting up sensors")
    entry = hass.data[DOMAIN][config_entry.entry_id]

    async def setupEntities(entry: dict) -> list:
        """Setup the entities."""
        sensors = []

        if "camData" in entry:
            camData: dict = entry["camData"]
            if "basic_info" in camData and "battery_percent" in camData["basic_info"]:
                LOGGER.debug("Adding tapoBatterySensor...")
                sensors.append(TapoBatterySensor(entry, hass, config_entry))

            if (
                camData.get("connectionInformation", False) is not False
                and camData["connectionInformation"] is not None
            ):
                if "ssid" in camData["connectionInformation"]:
                    LOGGER.debug("Adding TapoSSIDSensor...")
                    sensors.append(TapoSSIDSensor(entry, hass, config_entry))
                if "link_type" in camData["connectionInformation"]:
                    LOGGER.debug("Adding TapoLinkTypeSensor...")
                    sensors.append(TapoLinkTypeSensor(entry, hass, config_entry))
                if "rssiValue" in camData["connectionInformation"]:
                    LOGGER.debug("Adding TapoRSSISensor...")
                    sensors.append(TapoRSSISensor(entry, hass, config_entry))

            if "sdCardData" in camData and len(camData["sdCardData"]) > 0:
                for hdd in camData["sdCardData"]:
                    for field in hdd:
                        LOGGER.debug(
                            "Adding TapoHDDSensor for disk %s and property %s...",
                            hdd["disk_name"],
                            field,
                        )
                        sensors.append(
                            TapoHDDSensor(
                                entry, hass, config_entry, hdd["disk_name"], field
                            )
                        )
            if (
                "basic_info" in camData
                and camData["basic_info"] is not None
                and "signal_level" in camData["basic_info"]
                and camData["basic_info"]["signal_level"] is not None
            ):
                sensors.append(TapoChimeSignalLevel(entry, hass, config_entry))

            if "rebootLastTime" in camData and camData["rebootLastTime"] is not None:
                LOGGER.debug("Adding TapoLastRebootTimeSensor...")
                sensors.append(TapoLastRebootTimeSensor(entry, hass, config_entry))

        sync_source = config_entry.data.get(
            RECORDINGS_SOURCE,
            config_entry.data.get("media_sync_source", RECORDINGS_SOURCE_SD),
        )
        if entry["controller"].isKLAP is False or sync_source == RECORDINGS_SOURCE_TAPO_CARE:
            sensors.append(TapoSyncSensor(entry, hass, config_entry))

        return sensors

    sensors = await setupEntities(entry)
    for childDevice in entry["childDevices"]:
        sensors.extend(await setupEntities(childDevice))

    async_add_entities(sensors)


class TapoRSSISensor(TapoSensorEntity):
    """Tapo RSSI sensor entity."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT

    def __init__(
        self, entry: dict, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> None:
        """Initialize the entity."""
        TapoSensorEntity.__init__(
            self,
            "RSSI",
            entry,
            hass,
            config_entry,
            "mdi:signal-variant",
            None,
        )

    async def async_update(self) -> None:
        await self._coordinator.async_request_refresh()

    def updateTapo(self, camData: dict | None) -> None:
        """Update the entity."""
        if (
            not camData
            or camData["connectionInformation"] is False
            or "rssiValue" not in camData["connectionInformation"]
        ):
            self._attr_native_value = STATE_UNAVAILABLE
        else:
            self._attr_native_value = camData["connectionInformation"]["rssiValue"]


class TapoLinkTypeSensor(TapoSensorEntity):
    """Tapo link type sensor entity."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: dict, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> None:
        """Initialize the entity."""
        TapoSensorEntity.__init__(
            self,
            "Link Type",
            entry,
            hass,
            config_entry,
            "mdi:connection",
            None,
        )

    async def async_update(self) -> None:
        """Update the entity."""
        await self._coordinator.async_request_refresh()

    def updateTapo(self, camData: dict | None) -> None:
        """Update the entity."""
        if (
            not camData
            or camData["connectionInformation"] is False
            or "link_type" not in camData["connectionInformation"]
        ):
            self._attr_native_value = STATE_UNAVAILABLE
        else:
            self._attr_native_value = camData["connectionInformation"]["link_type"]


class TapoChimeSignalLevel(TapoSensorEntity):
    """Tapo Chime Signal Level sensor entity."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: dict, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> None:
        """Initialize the entity."""
        TapoSensorEntity.__init__(
            self,
            "Signal Level",
            entry,
            hass,
            config_entry,
            "mdi:signal",
            None,
        )

    async def async_update(self) -> None:
        """Update the entity."""
        await self._coordinator.async_request_refresh()

    def updateTapo(self, camData: dict | None) -> None:
        """Update the entity."""
        if (
            not camData
            or camData["basic_info"] is False
            or camData["basic_info"] is None
            or "signal_level" not in camData["basic_info"]
        ):
            self._attr_native_value = STATE_UNAVAILABLE
        else:
            self._attr_native_value = camData["basic_info"]["signal_level"]


class TapoSSIDSensor(TapoSensorEntity):
    """Tapo SSID sensor entity."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: dict, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> None:
        """Initialize the entity."""
        TapoSensorEntity.__init__(
            self,
            "Network SSID",
            entry,
            hass,
            config_entry,
            "mdi:wifi",
            None,
        )

    async def async_update(self) -> None:
        """Update the entity."""
        await self._coordinator.async_request_refresh()

    def updateTapo(self, camData: dict | None) -> None:
        """Update the entity."""
        if (
            not camData
            or camData["connectionInformation"] is False
            or "ssid" not in camData["connectionInformation"]
        ):
            self._attr_native_value = STATE_UNAVAILABLE
        else:
            self._attr_native_value = camData["connectionInformation"]["ssid"]


class TapoBatterySensor(TapoSensorEntity):
    """Tapo battery sensor entity."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self, entry: dict, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> None:
        """Initialize the entity."""
        TapoSensorEntity.__init__(
            self,
            "Battery",
            entry,
            hass,
            config_entry,
            "mdi:battery",
            SensorDeviceClass.BATTERY,
        )

    async def async_update(self) -> None:
        """Update the entity."""
        await self._coordinator.async_request_refresh()

    def updateTapo(self, camData: dict | None) -> None:
        """Update the entity."""
        if not camData:
            self._attr_native_value = STATE_UNAVAILABLE
        else:
            self._attr_native_value = camData["basic_info"]["battery_percent"]


class TapoHDDSensor(TapoSensorEntity):
    """Tapo HDD sensor entities."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    _state_parser = re.compile(
        r"\A\s*(?P<value>[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+)))\s*(?P<unit>[^0-9.].*?)\s*\Z"
    )

    def __init__(
        self,
        entry: dict,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        sensorName: str,
        sensorProperty: str,
    ) -> None:
        """Initialize the entity."""
        self._sensor_name = sensorName
        self._sensor_property = sensorProperty
        TapoSensorEntity.__init__(
            self,
            f"Disk {sensorName} {sensorProperty}",
            entry,
            hass,
            config_entry,
            "mdi:sd",
            None,
        )

    async def async_update(self) -> None:
        """Update the entity."""
        await self._coordinator.async_request_refresh()

    def updateTapo(self, camData: dict | None) -> None:
        """Update the entity."""
        state = STATE_UNAVAILABLE
        if camData and "sdCardData" in camData and len(camData["sdCardData"]) > 0:
            for hdd in camData["sdCardData"]:
                if hdd["disk_name"] == self._sensor_name:
                    state = hdd[self._sensor_property]
        if "space" in self._sensor_property and (
            match := __class__._state_parser.search(state)
        ):
            unit = match["unit"]
            if unit in UnitOfInformation:
                self._attr_device_class = SensorDeviceClass.DATA_SIZE
                self._attr_native_unit_of_measurement = UnitOfInformation(unit)
                self._attr_suggested_unit_of_measurement = UnitOfInformation.GIGABYTES
                state = match["value"]
        self._attr_native_value = state


class TapoSyncSensor(TapoSensorEntity):
    """Tapo sync sensor entities."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: dict, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> None:
        """Initialize the entity."""
        TapoSensorEntity.__init__(
            self,
            "Recordings Synchronization",
            entry,
            hass,
            config_entry,
            "mdi:sync",
            None,
        )

    async def async_update(self) -> None:
        """Update the entity."""
        await self._coordinator.async_request_refresh()

    def updateTapo(self, camData: dict | None) -> None:
        """Update the entity."""
        data = self._hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id, {})
        if not data:
            return

        enable_media_sync = data.get(ENABLE_MEDIA_SYNC, False)
        runningMediaSync = data.get("runningMediaSync", False)

        sync_source = self._config_entry.data.get(
            RECORDINGS_SOURCE,
            self._config_entry.data.get("media_sync_source", RECORDINGS_SOURCE_SD),
        )

        LOGGER.debug("Enable Media Sync: %s", enable_media_sync)
        LOGGER.debug("Running media sync: %s", runningMediaSync)
        LOGGER.debug("Sync Source: %s", sync_source)

        if sync_source == RECORDINGS_SOURCE_TAPO_CARE:
            cold_storage_path = self._config_entry.data.get(MEDIA_SYNC_COLD_STORAGE_PATH)
            storage_exists = bool(
                cold_storage_path
                and os.path.exists(cold_storage_path)
                and os.path.isdir(cold_storage_path)
            )

            if not enable_media_sync:
                self._attr_native_value = "Tapo Care - Disabled"
                self._attr_icon = "mdi:cloud-off-outline"
            elif not storage_exists:
                self._attr_native_value = "Tapo Care - Storage Not Found"
                self._attr_icon = "mdi:cloud-alert"
            elif runningMediaSync:
                self._attr_native_value = "Tapo Care - Cleaning"
                self._attr_icon = "mdi:cloud-sync"
            else:
                self._attr_native_value = "Tapo Care - Idle"
                self._attr_icon = "mdi:cloud-check"

            attributes = {
                "storage_mode": RECORDINGS_SOURCE_TAPO_CARE,
                "sync_enabled": bool(enable_media_sync),
                "cold_storage_path": cold_storage_path,
                "cold_storage_found": storage_exists,
            }
            cleanup_time = self._config_entry.data.get(TAPO_CARE_CLEANUP_TIME)
            if cleanup_time:
                attributes["cleanup_time"] = cleanup_time
                try:
                    ch, cm = map(int, cleanup_time.split(":"))
                    local_now = dt_util.now()
                    target_today = local_now.replace(
                        hour=ch, minute=cm, second=0, microsecond=0
                    )
                    if local_now < target_today:
                        next_run = target_today
                    else:
                        next_run = target_today + datetime.timedelta(days=1)
                    attributes["next_cleanup"] = next_run.isoformat()
                except Exception:
                    pass
        else:
            LOGGER.debug("Initial Media Scan: %s", data.get("initialMediaScanDone"))
            LOGGER.debug("Media Sync Available: %s", data.get("mediaSyncAvailable"))
            LOGGER.debug("Download Progress: %s", data.get("downloadProgress"))
            LOGGER.debug("Running media sync: %s", data.get("runningMediaSync"))
            LOGGER.debug("Media Sync Schedueled: %s", data.get("mediaSyncScheduled"))
            LOGGER.debug("Media Sync Ran Once: %s", data.get("mediaSyncRanOnce"))

            if enable_media_sync or runningMediaSync is True:
                if not data.get("initialMediaScanDone") or (
                    data.get("initialMediaScanDone") and not data.get("mediaSyncRanOnce")
                ):
                    self._attr_native_value = "Starting"
                    self._attr_icon = "mdi:sync"
                elif not data.get("mediaSyncAvailable"):
                    self._attr_native_value = "No Recordings Found"
                    self._attr_icon = "mdi:sd"
                elif data.get("downloadProgress"):
                    if data["downloadProgress"] == "Finished download":
                        self._attr_native_value = "Idle"
                        self._attr_icon = "mdi:sd"
                    else:
                        self._attr_native_value = data["downloadProgress"]
                        self._attr_icon = "mdi:sync"
                else:
                    self._attr_native_value = "Idle"
                    self._attr_icon = "mdi:sd"
            else:
                self._attr_native_value = "Idle"
                self._attr_icon = "mdi:sd"

            attributes = {
                "storage_mode": RECORDINGS_SOURCE_SD,
                "sync_enabled": bool(enable_media_sync),
                "media_sync_available": data.get("mediaSyncAvailable", True),
                "download_progress": data.get("downloadProgress"),
            }
            if data.get("sdDownloadMethod"):
                attributes["sd_download_method"] = data["sdDownloadMethod"]
            if data.get("lastDownloadWarning"):
                attributes["last_download_warning"] = data["lastDownloadWarning"]

        media_sync_hours = self._config_entry.data.get(MEDIA_SYNC_HOURS)
        if media_sync_hours:
            attributes["retention_hours"] = media_sync_hours
        last_cleanup = data.get("lastMediaCleanup")
        if last_cleanup:
            attributes["last_cleanup"] = (
                dt_util.utc_from_timestamp(last_cleanup).isoformat()
            )
        attributes["last_deleted_recordings"] = data.get("lastDeletedRecordings", [])
        attributes["last_deleted_recordings_total"] = data.get(
            "lastDeletedRecordingsTotal", 0
        )
        attributes["last_cleanup_result"] = data.get(
            "lastCleanupResult", "No cleanup performed yet"
        )
        self._attr_extra_state_attributes = attributes


class TapoLastRebootTimeSensor(TapoSensorEntity):
    """Tapo last reboot time sensor."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: dict, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> None:
        """Initialize the entity."""
        TapoSensorEntity.__init__(
            self,
            "Last Automatic Reboot Time",
            entry,
            hass,
            config_entry,
            "mdi:restart",
            SensorDeviceClass.TIMESTAMP,
        )

    async def async_update(self) -> None:
        """Update the entity."""
        await self._coordinator.async_request_refresh()

    def updateTapo(self, camData: dict | None) -> None:
        """Update the entity."""
        if (
            not camData
            or "rebootLastTime" not in camData
            or camData["rebootLastTime"] is None
        ):
            self._attr_native_value = STATE_UNAVAILABLE
            return

        try:
            reboot_ts = int(camData["rebootLastTime"])
        except (TypeError, ValueError):
            self._attr_native_value = STATE_UNAVAILABLE
            return

        if reboot_ts <= 0:
            self._attr_native_value = STATE_UNAVAILABLE
            return

        self._attr_native_value = dt_util.utc_from_timestamp(reboot_ts)
