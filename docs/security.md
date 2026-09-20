# cQuant Security Guide

## Authentication Model

The API server (`cquant.api_server`) enforces Bearer token authentication on
all `/api/v1/*` routes (health endpoints are exempt).

**Default: deny.** If `CQUANT_API_KEY` is not set, every authenticated
endpoint — including `/api/v1/trading/*` — returns `503 Service Unavailable`
with guidance on how to configure a key. This is a deliberate fail-closed
design: an unconfigured deployment is an unreachable deployment.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CQUANT_API_KEY` | *(unset)* | API key. All requests must send `Authorization: Bearer <key>`. |
| `CQUANT_AUTH_MODE` | `strict` | `dev` allows unauthenticated access to non-trading endpoints when no key is set (one-time warning logged). |

### Key Generation

```bash
python -m cquant.cli.main auth generate-key
# === Generated API Key ===
#   <32-char key>
export CQUANT_API_KEY='<key>'
```

Clients then send `Authorization: Bearer <key>` on every request. Comparison
uses `hmac.compare_digest` (constant-time).

### Dev Mode (local development only)

```bash
export CQUANT_AUTH_MODE=dev
```

Preserves the historical local-development experience: without a key,
non-trading endpoints respond normally (a warning is logged once per process).
Trading endpoints still require a key even in dev mode.

**Never set `CQUANT_AUTH_MODE=dev` in production.**

## Key Rotation

1. Generate a new key: `python -m cquant.cli.main auth generate-key`
2. Update `CQUANT_API_KEY` in the server environment.
3. Restart the API server (single-key model; brief downtime during restart is
   expected — schedule accordingly).
4. Update all clients with the new key.

Because the model is single-key, rotation is atomic on restart. A staggered
dual-key window is a future enhancement; for now prefer rotating during a
maintenance window.

## Deployment Checklist

Before exposing the API server beyond localhost:

- [ ] `CQUANT_API_KEY` is set to a generated 32-char key (`cquant auth generate-key`)
- [ ] `CQUANT_AUTH_MODE` is unset or `strict` — **never `dev`**
- [ ] The server binds only to the intended interface (`127.0.0.1` unless external access is required)
- [ ] HTTPS is terminated by a reverse proxy (nginx/caddy) — never serve auth tokens over plain HTTP
- [ ] The reverse proxy restricts `/api/v1/trading/*` to trusted networks regardless of API key
- [ ] Related secrets (`TUSHARE_TOKEN`, LLM API keys) are provided via environment/secret manager, not committed to the repo
- [ ] Key rotation schedule is defined (recommended: at least quarterly)
