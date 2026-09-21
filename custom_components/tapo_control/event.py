from typing import Optional
from homeassistant.components.event import EventEntity, EventDeviceClass
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, LOGGER
from .utils import build_device_info


async def async_setup_entry(hass, config_entry, async_add_entities):
    LOGGER.debug("Setting up event entity for motion.")
    listener = EventsEntityListener(async_add_entities, hass, config_entry)
    hass.data[DOMAIN][config_entry.entry_id]["eventsEntityListener"] = listener

    if hass.data[DOMAIN][config_entry.entry_id].get("events"):
        listener.createEventEntities()

    return True


class EventsEntityListener:
    def __init__(self, async_add_entities, hass, config_entry):
        LOGGER.debug("EventsEntityListener init")
        self.metaData = hass.data[DOMAIN][config_entry.entry_id]
        self.async_add_entities = async_add_entities

    def createEventEntities(self):
        LOGGER.debug("Creating event entities for motion.")
        events = self.metaData.get("events")
        if not events:
            return
        name = self.metaData.get("name")
        camData = self.metaData.get("camData")
        entities = {}
        if camData:
            entities = {
                f"{event.uid}_event": TapoMotionEvent(event.uid, events, name, camData)
                for event in events.get_platform("binary_sensor")
            }
            self.async_add_entities(entities.values())
        uids_by_platform = events.get_uids_by_platform("binary_sensor")

        @callback
        def async_check_entities():
            LOGGER.debug("async_check_event_entities")
            nonlocal uids_by_platform
            if not (missing := uids_by_platform.difference(
                {uid.replace("_event", "") for uid in entities}
            )):
                return
            currentCamData = self.metaData.get("camData")
            if not currentCamData:
                LOGGER.debug("async_check_event_entities - camData not ready; will retry")
                return
            new_entities: dict[str, TapoMotionEvent] = {
                f"{uid}_event": TapoMotionEvent(uid, events, name, currentCamData)
                for uid in missing
            }
            if new_entities:
                entities.update(new_entities)
                self.async_add_entities(new_entities.values())

        events.async_add_listener(async_check_entities)


class TapoMotionEvent(EventEntity):
    def __init__(self, uid, events, name, camData):
        LOGGER.debug("TapoMotionEvent - init - start")
        EventEntity.__init__(self)
        self._attr_unique_id = f"{uid}_event"
        self._name = name
        self._attributes = camData.get("basic_info", {})
        self.uid = uid
        self.events = events
        event = events.get_uid(uid)

        self._attr_device_class = EventDeviceClass.MOTION
        self._attr_event_types = ["motion"]
        self._attr_translation_key = "motion"
        self._attr_entity_category = event.entity_category if event else None
        self._attr_entity_registry_enabled_default = event.entity_enabled if event else True
        self._attr_name = f"{self._name} {event.name}" if event else f"{self._name} Motion"
        self._last_state = bool(event.value) if event else False
        self._attr_enabled = event.entity_enabled if event else True
        LOGGER.debug("TapoMotionEvent - init - end")

    @property
    def entity_registry_enabled_default(self) -> bool:
        if (event := self.events.get_uid(self.uid)) is not None:
            return event.entity_enabled
        return self._attr_enabled

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self._attributes)

    @callback
    def _async_handle_event(self):
        if (event := self.events.get_uid(self.uid)) is not None:
            is_on = bool(event.value)
            if is_on and not self._last_state:
                LOGGER.debug("TapoMotionEvent '%s' triggered motion event", self._attr_name)
                self._trigger_event("motion")
                self.async_write_ha_state()
            self._last_state = is_on

    async def async_added_to_hass(self):
        self.async_on_remove(self.events.async_add_listener(self._async_handle_event))
