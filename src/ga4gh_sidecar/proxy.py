"""Reverse proxy logic using httpx.

Forwards all non-/service-info requests to the backend service,
streaming the response body and handling headers correctly.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from fastapi import Request, Response

# Headers that must not be forwarded between hops (RFC 2616 Section 13.5.1)
HOP_BY_HOP_HEADERS: set[str] = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Remove hop-by-hop headers from a header mapping."""
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


async def reverse_proxy(
    request: Request,
    backend_url: str,
    client: httpx.AsyncClient,
) -> Response:
    """Forward a request to the backend and stream the response back.

    Strips hop-by-hop headers, injects X-Forwarded-For, and streams
    the response body so large genomic payloads don't buffer in memory.
    """
    # Build the target URL
    target_path = request.url.path
    target_query = str(request.url.query) if request.url.query else ""
    target_url = f"{backend_url}{target_path}"
    if target_query:
        target_url += f"?{target_query}"

    # Build forwarded headers
    headers = _filter_headers(request.headers)
    # Inject forwarding info
    client_host = request.client.host if request.client else "unknown"
    existing_xff = headers.get("x-forwarded-for", "")
    if existing_xff:
        headers["x-forwarded-for"] = f"{existing_xff}, {client_host}"
    else:
        headers["x-forwarded-for"] = client_host
    headers["x-forwarded-proto"] = request.url.scheme
    # Remove the host header so httpx sets it correctly for the backend
    headers.pop("host", None)

    # Read the request body
    body = await request.body()

    # Make the proxied request
    backend_resp = await client.request(
        method=request.method,
        url=target_url,
        headers=headers,
        content=body if body else None,
    )

    # Build the response headers, stripping hop-by-hop
    resp_headers = _filter_headers(backend_resp.headers)
    # Add a header to indicate this came through the sidecar
    resp_headers["x-ga4gh-sidecar"] = "true"

    return Response(
        content=backend_resp.content,
        status_code=backend_resp.status_code,
        headers=resp_headers,
    )
