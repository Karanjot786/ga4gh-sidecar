"""Tests for the cache state machine and ServiceInfoCache lifecycle."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ga4gh_sidecar.merger import CacheState, ServiceInfoCache


class TestCacheState:
    """Test cache state transitions during the polling lifecycle."""

    def _make_cache(self, client: httpx.AsyncClient | None = None) -> ServiceInfoCache:
        """Create a cache instance with defaults for testing."""
        return ServiceInfoCache(
            sidecar_config={"id": "org.test", "name": "Test"},
            backend_url="http://localhost:9090",
            client=client or httpx.AsyncClient(),
            poll_interval=30,
            backend_timeout=5,
        )

    def test_initial_state_is_cold(self):
        """Cache should start in COLD state."""
        cache = self._make_cache()
        assert cache.cache_state == CacheState.COLD

    def test_last_fetch_age_is_none_when_cold(self):
        """last_fetch_age_seconds should be None before any fetch."""
        cache = self._make_cache()
        assert cache.last_fetch_age_seconds is None
        assert cache.last_poll_time is None

    @pytest.mark.asyncio
    async def test_transitions_to_warm_on_success(self):
        """COLD → WARMING → WARM on successful first poll."""
        mock_response = httpx.Response(
            200,
            json={"id": "org.backend", "storage": ["s3"]},
            request=httpx.Request("GET", "http://localhost:9090/service-info"),
        )
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_response)

        cache = self._make_cache(client=client)
        assert cache.cache_state == CacheState.COLD

        await cache._poll_backend()

        assert cache.cache_state == CacheState.WARM
        assert cache.is_backend_healthy is True
        assert cache.last_fetch_age_seconds is not None
        assert cache.last_fetch_age_seconds >= 0

    @pytest.mark.asyncio
    async def test_transitions_to_error_on_failure(self):
        """COLD → WARMING → ERROR on failed first poll."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        cache = self._make_cache(client=client)
        await cache._poll_backend()

        assert cache.cache_state == CacheState.ERROR
        assert cache.is_backend_healthy is False

    @pytest.mark.asyncio
    async def test_warm_to_refreshing_on_subsequent_poll(self):
        """WARM → REFRESHING → WARM on subsequent successful poll."""
        mock_response = httpx.Response(
            200,
            json={"id": "org.backend"},
            request=httpx.Request("GET", "http://localhost:9090/service-info"),
        )
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_response)

        cache = self._make_cache(client=client)

        # First poll: COLD → WARM
        await cache._poll_backend()
        assert cache.cache_state == CacheState.WARM

        # Second poll: WARM → REFRESHING → WARM
        await cache._poll_backend()
        assert cache.cache_state == CacheState.WARM

    @pytest.mark.asyncio
    async def test_error_to_warming_on_retry(self):
        """ERROR → WARMING → WARM when retry succeeds."""
        mock_response = httpx.Response(
            200,
            json={"id": "org.backend"},
            request=httpx.Request("GET", "http://localhost:9090/service-info"),
        )
        client = AsyncMock(spec=httpx.AsyncClient)

        # First poll fails
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        cache = self._make_cache(client=client)
        await cache._poll_backend()
        assert cache.cache_state == CacheState.ERROR

        # Second poll succeeds
        client.get = AsyncMock(return_value=mock_response)
        await cache._poll_backend()
        assert cache.cache_state == CacheState.WARM

    @pytest.mark.asyncio
    async def test_cached_response_uses_sidecar_config_when_cold(self):
        """Before any poll, cached response should be the sidecar config."""
        cache = self._make_cache()
        response = cache.cached_response
        assert response["id"] == "org.test"
        assert response["name"] == "Test"

    @pytest.mark.asyncio
    async def test_fallback_serves_config_on_error(self):
        """When backend fails and fallback is serve_config_only, serve config."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        cache = ServiceInfoCache(
            sidecar_config={"id": "org.test", "name": "Test"},
            backend_url="http://localhost:9090",
            client=client,
            fallback="serve_config_only",
        )
        await cache._poll_backend()

        response = cache.cached_response
        assert response["id"] == "org.test"
        assert cache.cache_state == CacheState.ERROR
