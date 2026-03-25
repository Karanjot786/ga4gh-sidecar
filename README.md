# ga4gh-sidecar

A reverse proxy that sits in front of any GA4GH service, takes over the `/service-info` endpoint, and leaves everything else alone.

```
Client → Sidecar (port 8080) → Backend (port 9090)
              |
              ├── GET /service-info  → merged response from config + backend
              ├── GET /health        → health status
              └── anything else      → forwarded to backend as-is
```

## Contents

- [Why this exists](#why-this-exists)
- [Get it running](#get-it-running)
- [How the merge works](#how-the-merge-works)
- [Merge modes](#merge-modes)
- [Plugins](#plugins)
- [Configuration](#configuration)
- [Full configuration reference](#full-configuration-reference)
- [Tests](#tests)
- [Sample /service-info output](#sample-service-info-output)
- [Performance](#performance)
- [Layout](#layout)
- [Full project structure (GSoC target)](#full-project-structure-gsoc-target)
- [License](#license)

## Why this exists

Every GA4GH implementation handles `/service-info` differently. Funnel returns one shape, Cromwell another, TESK another. If you run three services, you get three inconsistent responses and three config files to keep in sync.

The sidecar pulls that logic out. You configure identity fields (who you are, how to reach you) in one YAML file, and the sidecar merges them with whatever the backend reports about its own capabilities. One place to update, consistent responses everywhere.

## Get it running

### Locally (no Docker)

```bash
pip install -e ".[dev]"

# start the mock TES backend
uvicorn mock_backend.app:app --port 9090 &

# start the sidecar
uvicorn ga4gh_sidecar.main:app --port 8080

# try it
curl http://localhost:8080/service-info | python -m json.tool
curl -X POST http://localhost:8080/tasks -H "Content-Type: application/json" -d '{"name": "test"}'
curl http://localhost:8080/health
```

### With Docker Compose

```bash
docker compose up --build

curl http://localhost:8080/service-info | python -m json.tool
```

## How the merge works

The sidecar polls the backend's `/service-info` every 15 seconds and combines it with your config. The precedence rules are explicit:

| Field | Winner | Reason |
|-------|--------|--------|
| `id`, `name`, `organization` | Your config | You control who you are |
| `contactUrl`, `environment` | Your config | Ops metadata is yours |
| `storage`, `workflow_type_versions` | Backend | It knows what it supports |
| `extension` (nested objects) | Both, recursively merged | Either side can add fields |
| `createdAt`, `updatedAt` | Backend | It tracks its own lifecycle |

The rules are two sets of field names in `merger.py`. Takes about 30 seconds to read.

## Merge modes

The sidecar supports two modes: `merge` (default) and `override`.

- `merge`: combines backend and config responses using the precedence rules above
- `override`: ignores the backend's `/service-info` entirely, returns config as the complete response

Set it in config:

```yaml
merge:
  mode: "merge"  # or "override"
```

Override mode covers deployments where the backend's `/service-info` is broken, stale, or nonexistent.

## Plugins

Plugins run after the merge and can add or change fields in the response. Writing one looks like this:

```python
from ga4gh_sidecar.plugins.base import SidecarPlugin

class MyPlugin(SidecarPlugin):
    def name(self) -> str:
        return "my-plugin"

    async def enrich_service_info(self, response: dict) -> dict:
        response.setdefault("extension", {})["custom_field"] = "value"
        return response
```

Ships with two plugins:
- **tes** — adds `storage` protocols (s3, gs, file, etc.)
- **wes** — adds `workflow_type_versions` (CWL, WDL)

Register your own via Python entry points in `pyproject.toml`.

## Configuration

See `config.yaml` for a full example. The minimum you need:

```yaml
service_info:
  id: "org.your-institute.tes"
  name: "Your TES"
  type:
    group: "org.ga4gh"
    artifact: "tes"
    version: "1.1.0"
  organization:
    name: "Your Institute"
    url: "https://your-institute.org"

plugins:
  - name: "tes"
    config:
      override_protocols: ["s3", "gs"]
```

## Full configuration reference

```yaml
# GA4GH ServiceInfo Sidecar Configuration
# Full reference: all fields with defaults and comments.

listen_port: 8080
backend_url: "http://localhost:9090"

tls:
  enabled: false
  cert_file: /etc/sidecar/tls/tls.crt
  key_file: /etc/sidecar/tls/tls.key
  min_version: "1.3"

service_info:
  id: "org.ga4gh.demo.tes"
  name: "GA4GH Demo TES (via Sidecar)"
  type:
    group: "org.ga4gh"
    artifact: "tes"
    version: "1.1.0"
  organization:
    name: "GA4GH Demo Institute"
    url: "https://demo.ga4gh.org"
  description: "A TES endpoint protected by the GA4GH ServiceInfo Sidecar."
  contactUrl: "mailto:ops@demo.ga4gh.org"
  documentationUrl: "https://docs.demo.ga4gh.org/tes"
  environment: "development"
  version: "2.0.0"

merge:
  mode: "merge"                    # "merge" or "override"
  poll_interval_seconds: 15
  backend_timeout_seconds: 5
  fallback: "serve_config_only"    # "serve_config_only" or "return_503"
  custom_schema_url: null          # optional: URL to a custom OpenAPI schema

security:
  rate_limit:
    requests_per_second: 100
    burst: 200
  cors:
    allowed_origins: ["*"]
    allowed_methods: ["GET", "OPTIONS"]
  oauth:
    enabled: false
    jwks_url: "https://auth.example.org/.well-known/jwks.json"

attestation:
  enabled: false
  tee_type: "auto"                 # "auto", "intel_tdx", "amd_sev_snp"
  provider: "mock"                 # "azure", "intel_trust_authority", "gcp", "mock"
  cache_ttl_seconds: 300
  model: "passport"                # "passport" or "background_check"

plugins:
  enabled:
    - "tes"
  config:
    tes:
      override_protocols: ["s3", "gs"]
```

## Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

25 tests, under a second. Config loading, merge precedence, plugin ordering, mock backend integration.

## Sample /service-info output

With the sidecar in front of the mock TES backend:

```json
{
  "id": "org.ga4gh.demo.tes",
  "name": "GA4GH Demo TES (via Sidecar)",
  "type": {
    "group": "org.ga4gh",
    "artifact": "tes",
    "version": "1.1.0"
  },
  "organization": {
    "name": "GA4GH Demo Institute",
    "url": "https://demo.ga4gh.org"
  },
  "contactUrl": "mailto:ops@demo.ga4gh.org",
  "environment": "development",
  "version": "2.0.0",
  "storage": ["s3", "ftp", "file"],
  "description": "A TES endpoint protected by the GA4GH ServiceInfo Sidecar.",
  "createdAt": "2025-01-01T00:00:00Z",
  "updatedAt": "2026-03-02T12:15:00+00:00"
}
```

`id`, `name`, `organization`, and `contactUrl` come from the sidecar config. `storage` and `createdAt` come from the backend. The `type` object is recursively merged.

## Performance

Benchmarks run on MacBook Pro, localhost, `hey` 1000 requests 10 concurrent.

| Path | p50 | p95 | p99 | RPS |
|------|-----|-----|-----|-----|
| `GET /service-info` (cached) | 0.9ms | 3.9ms | 41.6ms | 5348 |
| `GET /tasks` (forwarded) | 10.3ms | 27.3ms | 64.0ms | 749 |

All 2000 requests returned 200.

The `/service-info` path serves a cached in-memory response with no backend call. The `/tasks` numbers include the mock backend's own response time. Sidecar proxy overhead on forwarded paths is the difference between these two baselines. Expect lower absolute latency on server hardware with a real backend over a local network.

## Layout

```
├── src/ga4gh_sidecar/
│   ├── main.py          # FastAPI app, routes, lifespan
│   ├── config.py        # Pydantic config models
│   ├── proxy.py         # httpx reverse proxy
│   ├── merger.py        # merge algorithm + background cache
│   └── plugins/
│       ├── base.py      # plugin ABC + chain
│       ├── tes.py       # TES storage plugin
│       └── wes.py       # WES workflow plugin
├── mock_backend/
│   └── app.py           # mock TES for testing
├── tests/               # pytest suite
├── config.yaml          # example config
├── Dockerfile           # multi-stage, non-root
├── docker-compose.yml   # sidecar + mock backend
└── pyproject.toml
```

## Full project structure (GSoC target)

```
ga4gh-sidecar/
├── src/ga4gh_sidecar/
│   ├── __init__.py
│   ├── app.py              # FastAPI application, lifespan, route mounting
│   ├── proxy.py            # ReverseProxy with httpx connection pooling
│   ├── merge.py            # deep merge algorithm
│   ├── allof.py            # allOf schema resolution
│   ├── cache.py            # ServiceInfoCache with state machine lifecycle
│   ├── config.py           # Pydantic config model, YAML loading
│   ├── health.py           # /health endpoint with cache state reporting
│   ├── logging.py          # structured JSON log formatter
│   ├── plugins/
│   │   ├── base.py         # SidecarPlugin ABC
│   │   ├── chain.py        # PluginChain executor
│   │   ├── tes.py          # TES storage plugin
│   │   ├── wes.py          # WES workflow plugin
│   │   └── attestation.py  # TEE attestation plugin (RATS RFC 9334)
│   └── security/
│       ├── tls.py          # TLS 1.3 termination
│       ├── rate_limit.py   # TokenBucketRateLimiter
│       └── oauth.py        # Bearer token validation
├── tests/
├── schemas/
│   ├── service-info-1.0.0.yaml
│   └── tes-1.1.0.yaml
├── helm/
│   └── ga4gh-sidecar/
├── Dockerfile
├── docker-compose.yaml
└── pyproject.toml
```

The current prototype implements `main.py`, `config.py`, `proxy.py`, `merger.py`, and `plugins/`. The remaining modules are part of the GSoC scope.

## License

Apache 2.0
