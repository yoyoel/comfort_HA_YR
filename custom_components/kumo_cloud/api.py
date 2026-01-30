"""API client for Kumo Cloud."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine

import random

import aiohttp
from aiohttp import ClientResponseError

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    API_BASE_URL,
    API_VERSION,
    API_APP_VERSION,
    TOKEN_REFRESH_INTERVAL,
    TOKEN_EXPIRY_MARGIN,
    MAX_CONCURRENT_REQUESTS,
    RETRY_ATTEMPTS,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_MAX,
    RATE_LIMIT_BACKOFF_BASE,
    RATE_LIMIT_BACKOFF_MAX,
)

_LOGGER = logging.getLogger(__name__)


class KumoCloudError(HomeAssistantError):
    """Base exception for Kumo Cloud."""


class KumoCloudAuthError(KumoCloudError):
    """Authentication error."""


class KumoCloudConnectionError(KumoCloudError):
    """Connection error."""


class KumoCloudRateLimitError(KumoCloudError):
    """Rate limit error (HTTP 429)."""

    def __init__(self, message: str, retry_after: float | None = None):
        """Initialize with optional retry_after hint."""
        super().__init__(message)
        self.retry_after = retry_after


class KumoCloudAPI:
    """Kumo Cloud API client."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the API client."""
        self.hass = hass
        self.session = async_get_clientsession(hass)
        self.base_url = API_BASE_URL
        self.username: str | None = None
        self._password: str | None = None  # Stored for re-login
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.token_expires_at: datetime | None = None
        # Rate limiting
        self._request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._consecutive_rate_limits = 0
        # Token refresh lock to prevent concurrent refreshes
        self._token_refresh_lock = asyncio.Lock()
        # Callback for token updates (to persist to config entry)
        self._on_token_update: Callable[[str, str], Coroutine[Any, Any, None]] | None = None

    def set_credentials(self, username: str, password: str) -> None:
        """Store credentials for potential re-login."""
        self.username = username
        self._password = password

    def set_token_update_callback(
        self, callback: Callable[[str, str], Coroutine[Any, Any, None]]
    ) -> None:
        """Set callback to be called when tokens are updated."""
        self._on_token_update = callback

    async def _notify_token_update(self) -> None:
        """Notify callback that tokens have been updated."""
        if self._on_token_update:
            try:
                await self._on_token_update(self.access_token, self.refresh_token)
            except Exception as err:
                _LOGGER.warning("Failed to persist token update: %s", err)

    async def login(self, username: str, password: str) -> dict[str, Any]:
        """Login to Kumo Cloud and return user data."""
        url = f"{self.base_url}/{API_VERSION}/login"
        headers = {
            "x-app-version": API_APP_VERSION,
            "Content-Type": "application/json",
        }
        data = {
            "username": username,
            "password": password,
            "appVersion": API_APP_VERSION,
        }

        try:
            async with self._request_semaphore:
                async with asyncio.timeout(10):
                    async with self.session.post(
                        url, headers=headers, json=data
                    ) as response:
                        # Check for rate limiting
                        if response.status == 429:
                            retry_after_header = response.headers.get("Retry-After")
                            retry_after = None
                            if retry_after_header:
                                try:
                                    retry_after = float(retry_after_header)
                                except ValueError:
                                    pass
                            self._consecutive_rate_limits += 1
                            calculated_backoff = self._calculate_backoff(
                                self._consecutive_rate_limits - 1,
                                RATE_LIMIT_BACKOFF_BASE,
                                RATE_LIMIT_BACKOFF_MAX,
                            )
                            final_retry_after = max(retry_after or 0, calculated_backoff)
                            raise KumoCloudRateLimitError(
                                f"Rate limited during login. Retry after {final_retry_after}s",
                                retry_after=final_retry_after,
                            )

                        if response.status == 403:
                            raise KumoCloudAuthError("Invalid username or password")
                        response.raise_for_status()
                        result = await response.json()

                        _LOGGER.debug("Login response: %s", json.dumps(result, indent=2))

                        # Validate response structure
                        if "token" not in result:
                            raise KumoCloudConnectionError(
                                f"Invalid API response: missing 'token' field. Response: {result}"
                            )

                        token_data = result["token"]
                        if "access" not in token_data or "refresh" not in token_data:
                            raise KumoCloudConnectionError(
                                f"Invalid token structure: {token_data}"
                            )

                        self.username = username
                        self._password = password  # Store for re-login
                        self.access_token = token_data["access"]
                        self.refresh_token = token_data["refresh"]
                        self.token_expires_at = datetime.now() + timedelta(
                            seconds=TOKEN_REFRESH_INTERVAL
                        )
                        self._consecutive_rate_limits = 0

                        # Notify callback of new tokens
                        await self._notify_token_update()

                        return result

        except asyncio.TimeoutError as err:
            _LOGGER.debug("Login timeout after 10 seconds")
            raise KumoCloudConnectionError("Connection timeout") from err
        except ClientResponseError as err:
            _LOGGER.debug("Login HTTP error: %s", err.status)
            if err.status == 403:
                raise KumoCloudAuthError("Invalid credentials") from err
            raise KumoCloudConnectionError(f"HTTP error: {err.status}") from err
        except (KumoCloudAuthError, KumoCloudConnectionError, KumoCloudRateLimitError):
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected login error: %s", err)
            raise KumoCloudConnectionError(f"Unexpected error: {err}") from err

    async def refresh_access_token(self) -> None:
        """Refresh the access token with retry logic.

        If refresh fails with 401 (token expired), attempts re-login with stored credentials.
        """
        if not self.refresh_token:
            # No refresh token - try re-login if we have credentials
            if self.username and self._password:
                _LOGGER.info("No refresh token, attempting re-login")
                await self.login(self.username, self._password)
                return
            raise KumoCloudAuthError("No refresh token available")

        url = f"{self.base_url}/{API_VERSION}/refresh"
        headers = {
            "x-app-version": API_APP_VERSION,
            "Content-Type": "application/json",
        }
        data = {"refresh": self.refresh_token}

        last_error: Exception | None = None

        for attempt in range(RETRY_ATTEMPTS):
            try:
                async with self._request_semaphore:
                    async with asyncio.timeout(10):
                        async with self.session.post(
                            url, headers=headers, json=data
                        ) as response:
                            # Check for rate limiting
                            if response.status == 429:
                                retry_after_header = response.headers.get("Retry-After")
                                retry_after = None
                                if retry_after_header:
                                    try:
                                        retry_after = float(retry_after_header)
                                    except ValueError:
                                        pass
                                self._consecutive_rate_limits += 1
                                calculated_backoff = self._calculate_backoff(
                                    self._consecutive_rate_limits - 1,
                                    RATE_LIMIT_BACKOFF_BASE,
                                    RATE_LIMIT_BACKOFF_MAX,
                                )
                                final_retry_after = max(retry_after or 0, calculated_backoff)
                                raise KumoCloudRateLimitError(
                                    f"Rate limited during token refresh. Retry after {final_retry_after}s",
                                    retry_after=final_retry_after,
                                )

                            if response.status == 401:
                                # Refresh token expired - try re-login
                                if self.username and self._password:
                                    _LOGGER.info(
                                        "Refresh token expired, attempting re-login"
                                    )
                                    await self.login(self.username, self._password)
                                    return
                                raise KumoCloudAuthError("Refresh token expired")

                            response.raise_for_status()
                            result = await response.json()

                            self.access_token = result["access"]
                            self.refresh_token = result["refresh"]
                            self.token_expires_at = datetime.now() + timedelta(
                                seconds=TOKEN_REFRESH_INTERVAL
                            )
                            self._consecutive_rate_limits = 0

                            # Notify callback of updated tokens
                            await self._notify_token_update()
                            return

            except KumoCloudRateLimitError:
                # Don't retry rate limit errors
                raise
            except KumoCloudAuthError:
                # Don't retry auth errors
                raise
            except asyncio.TimeoutError:
                last_error = KumoCloudConnectionError("Connection timeout during refresh")
                _LOGGER.debug(
                    "Token refresh timeout (attempt %d/%d)",
                    attempt + 1, RETRY_ATTEMPTS
                )
            except ClientResponseError as err:
                if err.status == 401:
                    # Refresh token expired - try re-login
                    if self.username and self._password:
                        _LOGGER.info("Refresh token expired, attempting re-login")
                        await self.login(self.username, self._password)
                        return
                    raise KumoCloudAuthError("Refresh token expired") from err
                last_error = KumoCloudConnectionError(
                    f"HTTP error during refresh: {err.status}"
                )
                _LOGGER.debug(
                    "Token refresh HTTP error %d (attempt %d/%d)",
                    err.status, attempt + 1, RETRY_ATTEMPTS
                )
            except aiohttp.ClientError as err:
                last_error = KumoCloudConnectionError(f"Connection error during refresh: {err}")
                _LOGGER.debug(
                    "Token refresh connection error (attempt %d/%d): %s",
                    attempt + 1, RETRY_ATTEMPTS, err
                )

            # Wait before retrying
            if attempt < RETRY_ATTEMPTS - 1:
                backoff = self._calculate_backoff(
                    attempt, RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX
                )
                _LOGGER.debug("Retrying token refresh in %.1fs...", backoff)
                await asyncio.sleep(backoff)

        # All retries exhausted
        if last_error:
            raise last_error
        raise KumoCloudConnectionError("Token refresh failed after retries")

    async def _ensure_token_valid(self) -> None:
        """Ensure access token is valid, refresh if needed.

        Uses a lock to prevent multiple concurrent refresh attempts.
        """
        if not self.access_token:
            raise KumoCloudAuthError("No access token available")

        if (
            self.token_expires_at
            and datetime.now() + timedelta(seconds=TOKEN_EXPIRY_MARGIN)
            >= self.token_expires_at
        ):
            # Use lock to prevent concurrent refresh attempts
            async with self._token_refresh_lock:
                # Re-check after acquiring lock (another task may have refreshed)
                if (
                    self.token_expires_at
                    and datetime.now() + timedelta(seconds=TOKEN_EXPIRY_MARGIN)
                    >= self.token_expires_at
                ):
                    await self.refresh_access_token()

    def _calculate_backoff(
        self, attempt: int, base: float, max_delay: float, jitter: bool = True
    ) -> float:
        """Calculate exponential backoff delay with optional jitter.

        Jitter helps prevent the "thundering herd" problem when multiple
        clients retry simultaneously.
        """
        delay = base * (2 ** attempt)
        delay = min(delay, max_delay)
        if jitter:
            # Add up to 25% random jitter
            delay = delay * (0.75 + random.random() * 0.5)
        return delay

    def _handle_response_status(self, response: aiohttp.ClientResponse) -> None:
        """Handle response status, raising exceptions for rate limits.

        Raises KumoCloudRateLimitError on 429, resets counter on success.
        """
        if response.status == 429:
            # Rate limited - extract retry_after if available
            retry_after_header = response.headers.get("Retry-After")
            if retry_after_header:
                try:
                    retry_after = float(retry_after_header)
                except ValueError:
                    retry_after = None
            else:
                retry_after = None

            # Calculate backoff based on consecutive rate limits
            self._consecutive_rate_limits += 1
            calculated_backoff = self._calculate_backoff(
                self._consecutive_rate_limits - 1,
                RATE_LIMIT_BACKOFF_BASE,
                RATE_LIMIT_BACKOFF_MAX,
            )

            # Use the larger of retry_after header or calculated backoff
            final_retry_after = max(retry_after or 0, calculated_backoff)

            raise KumoCloudRateLimitError(
                f"Rate limited by API (429). Retry after {final_retry_after}s",
                retry_after=final_retry_after,
            )

        # Reset consecutive rate limit counter on success
        if response.status < 400:
            self._consecutive_rate_limits = 0

    async def _request(
        self, method: str, endpoint: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make an authenticated request to the API with retry logic."""
        method_upper = method.upper()
        if method_upper not in ("GET", "POST"):
            raise ValueError(f"Unsupported HTTP method: {method}")

        await self._ensure_token_valid()

        url = f"{self.base_url}/{API_VERSION}{endpoint}"
        headers = {
            "x-app-version": API_APP_VERSION,
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None

        for attempt in range(RETRY_ATTEMPTS):
            try:
                async with self._request_semaphore:
                    async with asyncio.timeout(10):
                        if method_upper == "GET":
                            async with self.session.get(url, headers=headers) as response:
                                self._handle_response_status(response)
                                response.raise_for_status()
                                return await response.json()
                        else:  # POST (already validated above)
                            async with self.session.post(
                                url, headers=headers, json=data
                            ) as response:
                                self._handle_response_status(response)
                                response.raise_for_status()
                                if response.content_type == "application/json":
                                    return await response.json()
                                return {}

            except KumoCloudRateLimitError:
                # Don't retry rate limit errors - propagate immediately
                raise
            except KumoCloudAuthError:
                # Don't retry auth errors - propagate immediately
                raise
            except asyncio.TimeoutError:
                last_error = KumoCloudConnectionError("Request timeout")
                _LOGGER.debug(
                    "Request timeout (attempt %d/%d): %s",
                    attempt + 1, RETRY_ATTEMPTS, endpoint
                )
            except ClientResponseError as err:
                if err.status == 401:
                    raise KumoCloudAuthError("Authentication failed") from err
                last_error = KumoCloudConnectionError(f"HTTP error: {err.status}")
                _LOGGER.debug(
                    "HTTP error %d (attempt %d/%d): %s",
                    err.status, attempt + 1, RETRY_ATTEMPTS, endpoint
                )
            except aiohttp.ClientError as err:
                last_error = KumoCloudConnectionError(f"Connection error: {err}")
                _LOGGER.debug(
                    "Connection error (attempt %d/%d): %s - %s",
                    attempt + 1, RETRY_ATTEMPTS, endpoint, err
                )

            # If we have more attempts, wait before retrying
            if attempt < RETRY_ATTEMPTS - 1:
                backoff = self._calculate_backoff(
                    attempt, RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX
                )
                _LOGGER.debug("Retrying in %.1fs...", backoff)
                await asyncio.sleep(backoff)

        # All retries exhausted
        if last_error:
            raise last_error
        raise KumoCloudConnectionError("Request failed after retries")

    async def get_account_info(self) -> dict[str, Any]:
        """Get account information."""
        return await self._request("GET", "/accounts/me")

    async def get_sites(self) -> list[dict[str, Any]]:
        """Get list of sites."""
        try:
            result = await self._request("GET", "/sites/")
            if not isinstance(result, list):
                _LOGGER.error("get_sites returned non-list: %s", type(result))
                raise KumoCloudConnectionError(f"Invalid sites response: expected list, got {type(result)}")
            return result
        except Exception as err:
            _LOGGER.exception("Error getting sites: %s", err)
            raise

    async def get_zones(self, site_id: str) -> list[dict[str, Any]]:
        """Get list of zones for a site."""
        return await self._request("GET", f"/sites/{site_id}/zones")

    async def get_device_details(self, device_serial: str) -> dict[str, Any]:
        """Get device details."""
        return await self._request("GET", f"/devices/{device_serial}")

    async def get_device_profile(self, device_serial: str) -> list[dict[str, Any]]:
        """Get device profile information."""
        return await self._request("GET", f"/devices/{device_serial}/profile")

    async def send_command(
        self, device_serial: str, commands: dict[str, Any]
    ) -> dict[str, Any]:
        """Send command to device."""
        data = {"deviceSerial": device_serial, "commands": commands}
        return await self._request("POST", "/devices/send-command", data)
