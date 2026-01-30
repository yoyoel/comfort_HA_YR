"""The Kumo Cloud integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    KumoCloudAPI,
    KumoCloudAuthError,
    KumoCloudConnectionError,
    KumoCloudRateLimitError,
)
from .const import COMMAND_DELAY, CONF_SITE_ID, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Kumo Cloud from a config entry."""

    # Create API client
    api = KumoCloudAPI(hass)

    # Store credentials for potential re-login
    api.set_credentials(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])

    # Set up callback to persist tokens when they're refreshed
    async def on_token_update(access_token: str, refresh_token: str) -> None:
        """Persist updated tokens to config entry."""
        _LOGGER.debug("Persisting updated tokens to config entry")
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                "access_token": access_token,
                "refresh_token": refresh_token,
            },
        )

    api.set_token_update_callback(on_token_update)

    # Initialize with stored tokens if available
    if "access_token" in entry.data:
        api.access_token = entry.data["access_token"]
        api.refresh_token = entry.data["refresh_token"]

    try:
        # Try to login or refresh tokens
        if not api.access_token:
            await api.login(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
        else:
            # Verify the token works by making a test request
            try:
                await api.get_account_info()
            except KumoCloudAuthError:
                # Token expired, try to login again (this will use stored credentials)
                _LOGGER.info("Stored tokens invalid, attempting fresh login")
                await api.login(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])

    except KumoCloudAuthError as err:
        raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
    except KumoCloudRateLimitError as err:
        raise ConfigEntryNotReady(
            f"Rate limited by API. Retry after {err.retry_after or 60}s"
        ) from err
    except KumoCloudConnectionError as err:
        raise ConfigEntryNotReady(f"Unable to connect: {err}") from err

    # Create the coordinator
    coordinator = KumoCloudDataUpdateCoordinator(hass, api, entry.data[CONF_SITE_ID], entry)

    # Fetch initial data so we have data when entities are added
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator in hass data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class KumoCloudDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Kumo Cloud data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: KumoCloudAPI,
        site_id: str,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api
        self.site_id = site_id
        self.config_entry = config_entry
        self.zones: list[dict[str, Any]] = []
        self.devices: dict[str, dict[str, Any]] = {}
        self.device_profiles: dict[str, list[dict[str, Any]]] = {}
        # Rate limit state tracking
        self._rate_limited = False
        self._rate_limit_until: datetime | None = None
        # Auth failure tracking
        self._auth_failures = 0
        self._max_auth_failures = 3  # Trigger reauth after this many consecutive failures

    @property
    def is_rate_limited(self) -> bool:
        """Return True if currently in rate limit backoff period."""
        if not self._rate_limited or not self._rate_limit_until:
            return False
        return datetime.now() < self._rate_limit_until

    def _check_and_clear_rate_limit(self) -> bool:
        """Check if rate limited, clear if expired. Returns True if still limited."""
        if not self._rate_limited or not self._rate_limit_until:
            return False
        if datetime.now() >= self._rate_limit_until:
            self._clear_rate_limit()
            return False
        return True

    @property
    def rate_limit_remaining_seconds(self) -> int:
        """Return remaining seconds in rate limit backoff, or 0 if not rate limited."""
        if not self.is_rate_limited or not self._rate_limit_until:
            return 0
        remaining = (self._rate_limit_until - datetime.now()).total_seconds()
        return max(0, int(remaining))

    def _set_rate_limit(self, retry_after: float) -> None:
        """Set rate limit backoff state."""
        self._rate_limited = True
        self._rate_limit_until = datetime.now() + timedelta(seconds=retry_after)
        _LOGGER.warning(
            "Rate limited by Kumo Cloud API. Backing off for %.0f seconds",
            retry_after,
        )

    def _clear_rate_limit(self) -> None:
        """Clear rate limit backoff state."""
        if self._rate_limited:
            _LOGGER.info("Rate limit backoff period ended")
        self._rate_limited = False
        self._rate_limit_until = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Kumo Cloud."""
        # Check if we're in rate limit backoff (and clear if expired)
        if self._check_and_clear_rate_limit():
            remaining = self.rate_limit_remaining_seconds
            _LOGGER.debug(
                "Still in rate limit backoff, %d seconds remaining. Using cached data.",
                remaining,
            )
            # Return cached data if available, otherwise raise UpdateFailed
            if self.data:
                return self.data
            raise UpdateFailed(
                f"Rate limited, {remaining}s remaining",
            )

        try:
            # Get zones for the site
            zones = await self.api.get_zones(self.site_id)

            # Get device details for each UNIQUE device serial SEQUENTIALLY
            # Multiple zones may share the same device (multi-zone units)
            devices = {}
            device_profiles = {}
            seen_serials: set[str] = set()

            for zone in zones:
                if "adapter" in zone and zone["adapter"]:
                    device_serial = zone["adapter"]["deviceSerial"]

                    # Skip if we've already fetched this device
                    if device_serial in seen_serials:
                        continue
                    seen_serials.add(device_serial)

                    # Get device details and profile sequentially (not in parallel)
                    device_detail = await self.api.get_device_details(device_serial)
                    device_profile = await self.api.get_device_profile(device_serial)

                    devices[device_serial] = device_detail
                    device_profiles[device_serial] = device_profile

            # Success - clear any rate limit state and auth failures
            self._clear_rate_limit()
            self._auth_failures = 0

            # Store the data for access by entities
            self.zones = zones
            self.devices = devices
            self.device_profiles = device_profiles

            return {
                "zones": zones,
                "devices": devices,
                "device_profiles": device_profiles,
            }

        except KumoCloudRateLimitError as err:
            retry_after = err.retry_after or 60
            self._set_rate_limit(retry_after)
            # Return cached data if available to keep entities available
            if self.data:
                _LOGGER.warning(
                    "Rate limited, using cached data. Will retry in %.0f seconds",
                    retry_after,
                )
            raise UpdateFailed(
                f"Rate limited by API. Retry after {retry_after}s",
            ) from err

        except KumoCloudAuthError as err:
            # Track consecutive auth failures
            self._auth_failures += 1
            _LOGGER.warning(
                "Authentication error (failure %d/%d): %s",
                self._auth_failures,
                self._max_auth_failures,
                err,
            )

            # If we've had too many consecutive auth failures, trigger reauth
            if self._auth_failures >= self._max_auth_failures:
                _LOGGER.error(
                    "Too many consecutive auth failures, triggering re-authentication"
                )
                self.config_entry.async_start_reauth(self.hass)
                raise UpdateFailed(
                    "Authentication failed repeatedly. Please re-authenticate."
                ) from err

            # The API will attempt re-login automatically on next request
            # Just raise UpdateFailed for now and let the next update try again
            raise UpdateFailed(f"Authentication failed: {err}") from err

        except KumoCloudConnectionError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def async_refresh_device(self, device_serial: str) -> None:
        """Refresh a specific device's data immediately."""
        # Skip refresh if we're in rate limit backoff
        if self.is_rate_limited:
            _LOGGER.debug(
                "Skipping device refresh for %s - in rate limit backoff (%ds remaining)",
                device_serial,
                self.rate_limit_remaining_seconds,
            )
            return

        try:
            # Get fresh device details
            device_detail = await self.api.get_device_details(device_serial)

            # Update the cached device data
            self.devices[device_serial] = device_detail

            # Also update the zone data if it contains the same info
            for zone in self.zones:
                if "adapter" in zone and zone["adapter"]:
                    if zone["adapter"]["deviceSerial"] == device_serial:
                        # Update adapter data with fresh device data
                        zone["adapter"].update(
                            {
                                "roomTemp": device_detail.get("roomTemp"),
                                "operationMode": device_detail.get("operationMode"),
                                "power": device_detail.get("power"),
                                "fanSpeed": device_detail.get("fanSpeed"),
                                "airDirection": device_detail.get("airDirection"),
                                "spCool": device_detail.get("spCool"),
                                "spHeat": device_detail.get("spHeat"),
                                "humidity": device_detail.get("humidity"),
                            }
                        )
                        break

            # Update the coordinator's data dict
            self.data = {
                "zones": self.zones,
                "devices": self.devices,
                "device_profiles": self.device_profiles,
            }

            # Notify all listeners that data has been updated
            self.async_update_listeners()

            _LOGGER.debug("Refreshed device %s data", device_serial)

        except KumoCloudRateLimitError as err:
            retry_after = err.retry_after or 60
            self._set_rate_limit(retry_after)
            _LOGGER.warning(
                "Rate limited during device refresh for %s. Deferring to next scheduled update.",
                device_serial,
            )
        except Exception as err:
            _LOGGER.warning("Failed to refresh device %s: %s", device_serial, err)


class KumoCloudDevice:
    """Representation of a Kumo Cloud device."""

    def __init__(
        self,
        coordinator: KumoCloudDataUpdateCoordinator,
        zone_id: str,
        device_serial: str,
    ) -> None:
        """Initialize the device."""
        self.coordinator = coordinator
        self.zone_id = zone_id
        self.device_serial = device_serial

    @property
    def zone_data(self) -> dict[str, Any]:
        """Get the zone data."""
        # Always get fresh data from coordinator
        for zone in self.coordinator.zones:
            if zone["id"] == self.zone_id:
                return zone
        return {}

    @property
    def device_data(self) -> dict[str, Any]:
        """Get the device data."""
        # Always get fresh data from coordinator
        return self.coordinator.devices.get(self.device_serial, {})

    @property
    def profile_data(self) -> list[dict[str, Any]]:
        """Get the device profile data."""
        # Always get fresh data from coordinator
        return self.coordinator.device_profiles.get(self.device_serial, [])

    @property
    def available(self) -> bool:
        """Return True if device is available."""
        adapter = self.zone_data.get("adapter", {})
        device_data = self.device_data

        # Check both adapter and device data for connection status
        adapter_connected = adapter.get("connected", False)
        device_connected = device_data.get("connected", adapter_connected)

        return device_connected

    @property
    def name(self) -> str:
        """Return the name of the device."""
        return self.zone_data.get("name", f"Zone {self.zone_id}")

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the device."""
        return f"{self.device_serial}_{self.zone_id}"

    async def send_command(self, commands: dict[str, Any]) -> None:
        """Send a command to the device and refresh status."""
        try:
            # Send the command
            await self.coordinator.api.send_command(self.device_serial, commands)
            _LOGGER.debug("Sent command to device %s: %s", self.device_serial, commands)

            # Reset auth failures on successful command
            self.coordinator._auth_failures = 0

            # Wait a moment for the command to be processed
            await asyncio.sleep(COMMAND_DELAY)

            # Refresh this specific device's data immediately
            # This may be skipped if rate limited, but the command was already sent
            await self.coordinator.async_refresh_device(self.device_serial)

        except KumoCloudRateLimitError as err:
            # Command was rate limited - set backoff and raise
            retry_after = err.retry_after or 60
            self.coordinator._set_rate_limit(retry_after)
            _LOGGER.error(
                "Command to device %s rate limited: %s", self.device_serial, err
            )
            raise
        except KumoCloudAuthError as err:
            # Auth failed even after API's automatic re-login attempt
            self.coordinator._auth_failures += 1
            _LOGGER.error(
                "Command to device %s auth failed (failure %d/%d): %s",
                self.device_serial,
                self.coordinator._auth_failures,
                self.coordinator._max_auth_failures,
                err,
            )
            # Trigger reauth if too many failures
            if self.coordinator._auth_failures >= self.coordinator._max_auth_failures:
                _LOGGER.error("Too many auth failures, triggering re-authentication")
                self.coordinator.config_entry.async_start_reauth(self.coordinator.hass)
            raise
        except Exception as err:
            _LOGGER.error(
                "Failed to send command to device %s: %s", self.device_serial, err
            )
            raise
