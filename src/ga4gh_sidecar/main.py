"""GA4GH ServiceInfo Sidecar — FastAPI application.

This is the main entry point. It sets up the reverse proxy,
loads plugins, starts background polling, and routes requests.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ga4gh_sidecar.config import SidecarConfig
from ga4gh_sidecar.merger import CacheState, ServiceInfoCache
from ga4gh_sidecar.plugins.base import PluginChain, SidecarPlugin
from ga4gh_sidecar.plugins.tes import TESPlugin
from ga4gh_sidecar.plugins.wes import WESPlugin
from ga4gh_sidecar.proxy import reverse_proxy


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter.

    Each log entry is a single-line JSON object with timestamp, level,
    logger, and message fields. Exception info is included when present.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.root.handlers = [handler]
logging.root.setLevel(logging.INFO)

logger = logging.getLogger("ga4gh_sidecar")

# Available built-in plugins
BUILTIN_PLUGINS: dict[str, type[SidecarPlugin]] = {
    "tes": TESPlugin,
    "wes": WESPlugin,
}

# Module-level state
_config: SidecarConfig | None = None
_cache: ServiceInfoCache | None = None
_proxy_client: httpx.AsyncClient | None = None
_poll_client: httpx.AsyncClient | None = None
_plugin_chain: PluginChain = PluginChain()


def _load_config() -> SidecarConfig:
    """Load config from YAML file or environment variable."""
    config_path = os.environ.get("SIDECAR_CONFIG_PATH", "config.yaml")
    logger.info(f"Loading config from {config_path}")
    return SidecarConfig.from_yaml(config_path)


def _build_plugin_chain(config: SidecarConfig) -> PluginChain:
    """Instantiate and register configured plugins."""
    chain = PluginChain()
    for entry in config.plugins:
        plugin_cls = BUILTIN_PLUGINS.get(entry.name)
        if plugin_cls:
            chain.add(plugin_cls())
            logger.info(f"Loaded plugin: {entry.name}")
        else:
            logger.warning(f"Unknown plugin: {entry.name}, skipping.")
    return chain


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    global _config, _cache, _proxy_client, _poll_client, _plugin_chain

    # Load configuration
    _config = _load_config()

    # Build plugin chain
    _plugin_chain = _build_plugin_chain(_config)
    plugin_configs = {p.name: p.config for p in _config.plugins}
    await _plugin_chain.startup_all(plugin_configs)

    # Create a persistent httpx client for backend polling
    _poll_client = httpx.AsyncClient(
        timeout=_config.merge.backend_timeout_seconds,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=30,
        ),
    )

    # Create the service info cache with background polling
    _cache = ServiceInfoCache(
        sidecar_config=_config.service_info.to_dict(),
        backend_url=_config.backend_url,
        client=_poll_client,
        poll_interval=_config.merge.poll_interval_seconds,
        backend_timeout=_config.merge.backend_timeout_seconds,
        fallback=_config.merge.fallback,
    )
    _cache.set_plugins(_plugin_chain.plugins)
    await _cache.start_polling()

    # Create a persistent httpx client for proxying with connection pooling
    _proxy_client = httpx.AsyncClient(
        timeout=30.0,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30,
        ),
    )

    logger.info(
        f"Sidecar started — listening on :{_config.listen_port}, "
        f"backend={_config.backend_url}"
    )

    yield

    # Shutdown
    await _cache.stop_polling()
    await _proxy_client.aclose()
    await _poll_client.aclose()
    logger.info("Sidecar stopped.")


app = FastAPI(
    title="GA4GH ServiceInfo Sidecar",
    description=(
        "A reverse proxy that intercepts /service-info requests for GA4GH services, "
        "dynamically merges metadata, and forwards all other traffic."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/service-info")
async def get_service_info() -> dict[str, Any]:
    """Return the merged /service-info response.

    This response is a combination of:
    1. The operator's sidecar configuration (identity, organization)
    2. The backend service's live /service-info (capabilities)
    3. Plugin-injected metadata (storage protocols, workflow types)

    The response is served from an in-memory cache that is refreshed
    by a background polling task.
    """
    if _cache is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Sidecar not initialized"},
        )
    return _cache.cached_response


@app.get("/health")
async def health_check():
    """Health check endpoint for Kubernetes probes.

    Returns 200 for all states except ERROR (503).
    COLD returns 200 because the sidecar can serve config-only
    responses before the first backend poll completes.
    """
    cache_state = _cache.cache_state if _cache else CacheState.COLD
    is_error = cache_state == CacheState.ERROR

    body = {
        "status": "degraded" if is_error else "healthy",
        "cache_state": cache_state.value,
        "backend_reachable": _cache.is_backend_healthy if _cache else False,
        "last_fetch_age_seconds": _cache.last_fetch_age_seconds if _cache else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if is_error:
        return JSONResponse(status_code=503, content=body)
    return body


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_all(request: Request, path: str):
    """Forward all non-/service-info requests to the backend unchanged.

    The sidecar adds X-Forwarded-For and X-Forwarded-Proto headers but
    does not modify the request body or other headers.
    """
    if _config is None or _proxy_client is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Sidecar not initialized"},
        )

    return await reverse_proxy(
        request=request,
        backend_url=_config.backend_url,
        client=_proxy_client,
    )


def cli():
    """CLI entry point for running the sidecar."""
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    os.environ["SIDECAR_CONFIG_PATH"] = config_path
    config = SidecarConfig.from_yaml(config_path)
    uvicorn.run(
        "ga4gh_sidecar.main:app",
        host="0.0.0.0",
        port=config.listen_port,
        log_level="info",
    )


if __name__ == "__main__":
    cli()
