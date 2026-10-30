"""Support for LifeSmart door locks."""

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import (
    DOMAIN,
    MANUFACTURER,
    HUB_ID_KEY,
    DEVICE_ID_KEY,
    DEVICE_DATA_KEY,
    DEVICE_VERSION_KEY,
    LIFESMART_SIGNAL_UPDATE_ENTITY,
    LOCK_TYPES,
)
from .entity import LifeSmartEntity
from .helpers import generate_unique_id

_LOGGER = logging.getLogger(__name__)

# EVTLO 携带实时开/关锁状态
STATE_KEY = "EVTLO"
# P1 的 valtag 指向中枢上执行开锁的 AI，例如 "/ai/<agt>/me/ai/AI1768483799"
UNLOCK_AI_KEY = "P1"
# 门锁通常 ~3 秒内上报开锁；超时未上报则放弃 "unlocking"，避免 HomeKit 卡住
UNLOCK_TIMEOUT_SECONDS = 15


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LifeSmart locks from a config entry."""
    hub = hass.data[DOMAIN][config_entry.entry_id]["hub"]
    exclude_devices, exclude_hubs = hub.get_exclude_config()

    async_add_entities(
        LifeSmartLock(device, hub.get_client(), config_entry.entry_id)
        for device in hub.get_devices()
        if device.get("devtype") in LOCK_TYPES
        and STATE_KEY in device.get(DEVICE_DATA_KEY, {})
        and device[DEVICE_ID_KEY] not in exclude_devices
        and device[HUB_ID_KEY] not in exclude_hubs
    )


def _unlock_ai(data: dict) -> str | None:
    """Return the hub AI id that unlocks the door, if the lock has one."""
    valtag = data.get(UNLOCK_AI_KEY, {}).get("valtag")
    if isinstance(valtag, str) and "/ai/" in valtag:
        return valtag.rstrip("/").rsplit("/", 1)[-1]
    return None


def _is_locked(sub_data: dict) -> bool:
    """type%2==1 means open; method 15 is an error report, not an unlock."""
    opened = sub_data.get("type", 0) & 0x01 == 1
    errored = (sub_data.get("val", 0) >> 12) & 0xF == 15
    return not opened or errored


class LifeSmartLock(LifeSmartEntity, LockEntity):
    """LifeSmart door lock. Unlocks by running the hub AI linked to P1."""

    _attr_has_entity_name = False

    def __init__(self, raw_device: dict[str, Any], client: Any, entry_id: str) -> None:
        """Initialize the lock."""
        super().__init__(raw_device, client)
        self._entry_id = entry_id
        data = self._raw_device.get(DEVICE_DATA_KEY, {})

        # 与 EVTLO 二元传感器同一 unique_id，以便接收同一实时更新信号
        self._attr_unique_id = generate_unique_id(
            self.devtype, self.agt, self.me, STATE_KEY
        )
        self._attr_name = self._name
        self._unlock_ai = _unlock_ai(data)
        self._attr_extra_state_attributes = {
            HUB_ID_KEY: self.agt,
            DEVICE_ID_KEY: self.me,
            "unlock_automation": self._unlock_ai,
        }
        self._attr_is_locked = _is_locked(data.get(STATE_KEY, {}))
        self._cancel_unlock_timeout = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information to link the entity to a single device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.agt, self.me)},
            name=self._device_name,
            manufacturer=MANUFACTURER,
            model=self.devtype,
            sw_version=str(self._raw_device.get(DEVICE_VERSION_KEY, "unknown")),
            via_device=(DOMAIN, self.agt),
        )

    async def async_added_to_hass(self) -> None:
        """Register callbacks when entity is added."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{LIFESMART_SIGNAL_UPDATE_ENTITY}_{self._attr_unique_id}",
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                LIFESMART_SIGNAL_UPDATE_ENTITY,
                self._handle_global_refresh,
            )
        )
        self.async_on_remove(self._clear_unlock_timeout)

    @callback
    def _clear_unlock_timeout(self) -> None:
        if self._cancel_unlock_timeout:
            self._cancel_unlock_timeout()
            self._cancel_unlock_timeout = None

    @callback
    def _unlock_timed_out(self, _now) -> None:
        self._cancel_unlock_timeout = None
        _LOGGER.warning(
            "%s did not report opening within %ss of unlock",
            self._name,
            UNLOCK_TIMEOUT_SECONDS,
        )
        self._attr_is_unlocking = False
        self.async_write_ha_state()

    @callback
    def _handle_update(self, new_data: dict) -> None:
        """Handle real-time EVTLO updates."""
        if new_data:
            self._clear_unlock_timeout()
            self._attr_is_unlocking = False
            self._attr_is_locked = _is_locked(new_data)
            self.async_write_ha_state()

    @callback
    def _handle_global_refresh(self) -> None:
        """Handle global data refresh to sync state."""
        devices = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {}).get("devices", [])
        current = next((d for d in devices if d[DEVICE_ID_KEY] == self.me), None)
        if current and STATE_KEY in current.get(DEVICE_DATA_KEY, {}):
            self._raw_device = current
            self._attr_is_locked = _is_locked(current[DEVICE_DATA_KEY][STATE_KEY])
            self.async_write_ha_state()

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock by running the hub automation that opens the door."""
        if not self._unlock_ai:
            raise HomeAssistantError(
                f"{self._name} has no unlock automation linked on the hub"
            )
        _LOGGER.info("Unlocking %s via hub AI %s", self._name, self._unlock_ai)
        await self._client.async_set_scene(self.agt, self._unlock_ai)
        # 实际状态以门锁上报的 EVTLO 为准
        self._attr_is_unlocking = True
        self.async_write_ha_state()
        self._clear_unlock_timeout()
        self._cancel_unlock_timeout = async_call_later(
            self.hass, UNLOCK_TIMEOUT_SECONDS, self._unlock_timed_out
        )

    async def async_lock(self, **kwargs: Any) -> None:
        """The C200 relocks itself; there is no remote lock command."""
        raise HomeAssistantError(f"{self._name} locks automatically")
