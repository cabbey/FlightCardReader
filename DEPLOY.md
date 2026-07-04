# Deployment Guide: Docker + Tailscale Funnel

This guide covers deploying Flight Card Scanner as a Docker container with
HTTPS provided by [Tailscale Funnel](https://tailscale.com/kb/1223/tailscale-funnel)
running on the host.

## Architecture

```
Internet (HTTPS :443)
        │
        ▼
┌─────────────────────────┐
│  Tailscale Funnel       │  ← TLS termination, auto-provisioned certs
│  (host-level daemon)    │
└───────────┬─────────────┘
            │ HTTP :80
            ▼
┌─────────────────────────┐
│  Docker container       │
│  flight-card-scanner    │  ← plain HTTP on port 80
│  volume: /data          │
└─────────────────────────┘
            │
            ▼
┌─────────────────────────┐
│  /data (bind mount)     │
│  config.json            │
│  myevent/               │
│    ├── images/          │
│    ├── flight_cards.db  │
│    └── known_fliers.tsv │
└─────────────────────────┘
```

Tailscale handles TLS certificates automatically (via Let's Encrypt) and
proxies decrypted HTTP traffic to `localhost:80` where the container is
listening. The container never sees TLS.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Docker Engine | Any recent version (20.10+) |
| Tailscale | v1.38.3+ installed **on the host** (not in the container) |
| Tailscale account | With MagicDNS and HTTPS enabled |
| Funnel node attribute | Added to your tailnet policy (done automatically on first `tailscale funnel` use) |

---

## 1. Build the image

```bash
cd FlightCardReader
docker build -t flight-card-scanner .
```

The multi-stage build produces a ~220 MB Alpine-based image with Python 3.12,
Pillow (JPEG/WebP), rapidfuzz, and all FastAPI dependencies.

---

## 2. Prepare the data volume

Create a directory on the host to hold the config and event data:

```bash
mkdir -p /srv/flight-cards
```

Create `/srv/flight-cards/config.json`:

```json
{
  "host": "0.0.0.0",
  "port": 80,
  "event_data_path": "./myevent",
  "event_name": "My Launch Event 2026",
  "event_date_range": {
    "start": "2026-07-04",
    "end": "2026-07-06"
  },
  "extraction_mode": "immediate",
  "extraction_endpoints": [
    { "url": "http://host.docker.internal:11434", "concurrency": 2 }
  ]
}
```

Key points:

- **`"port": 80`** — the container listens on port 80 (no SSL).
- **`"event_data_path": "./myevent"`** — resolved relative to the config file,
  so this becomes `/srv/flight-cards/myevent` inside the container (mapped via
  the bind mount).
- **No `ssl_certfile` or `ssl_keyfile`** — TLS is handled by Tailscale Funnel
  on the host.
- **`extraction_endpoints`** — point to your Ollama instance. Use
  `host.docker.internal` to reach a service running on the Docker host, or a
  tailnet hostname if Ollama is on another machine.

Optionally add a known fliers roster:

```json
{
  "known_fliers_path": "./known_fliers.tsv"
}
```

---

## 3. Run the container

```bash
docker run -d \
  --name flight-card-scanner \
  --restart unless-stopped \
  -v /srv/flight-cards:/data \
  -p 127.0.0.1:80:80 \
  flight-card-scanner
```

Notes:

- **`-p 127.0.0.1:80:80`** binds to localhost only — the app is not directly
  exposed to the network. Tailscale Funnel handles public access.
- **`--restart unless-stopped`** ensures the container comes back after reboot.
- The container creates `myevent/images/` and `myevent/flight_cards.db`
  automatically on first run.

Verify it's running:

```bash
curl -s http://localhost:80/ | head -5
```

---

## 4. Configure Tailscale Funnel

Tailscale must already be running and authenticated on the host:

```bash
tailscale status   # confirm the host is connected to your tailnet
```

### Expose via Funnel (public internet access)

```bash
sudo tailscale funnel --bg localhost:80
```

This does the following:

1. Provisions a TLS certificate for your node's DNS name
   (e.g. `myhost.tail1234.ts.net`).
2. Starts a persistent background reverse proxy:
   - Internet → `https://myhost.tail1234.ts.net:443` (HTTPS)
   - Funnel relay → your host (encrypted WireGuard tunnel)
   - Host Tailscale daemon → terminates TLS → forwards plain HTTP to `localhost:80`
   - Docker container handles the request.

Confirm it's working:

```bash
tailscale funnel status
```

Expected output:

```
https://myhost.tail1234.ts.net:443 (Funnel on)
|-- / proxy http://127.0.0.1:80
```

Your app is now publicly accessible at `https://myhost.tail1234.ts.net`.

### Alternative: Tailscale Serve (tailnet-only, no public access)

If you only need access from devices on your tailnet (not the public internet):

```bash
sudo tailscale serve --bg localhost:80
```

Same mechanics, but traffic is restricted to authenticated tailnet members.

### Using a different port

Funnel only supports ports **443**, **8443**, and **10000**. To use port 8443:

```bash
sudo tailscale funnel --bg --https=8443 localhost:80
```

The app will be reachable at `https://myhost.tail1234.ts.net:8443`.

---

## 5. Controlling the Funnel

| Action | Command |
|--------|---------|
| Check status | `tailscale funnel status` |
| Stop Funnel | `sudo tailscale funnel --https=443 off` |
| Reset all Funnel config | `sudo tailscale funnel reset` |
| Switch to tailnet-only | `sudo tailscale serve --bg localhost:80` |

The `--bg` flag makes the Funnel persistent — it survives Tailscale restarts
and host reboots. Without `--bg`, the Funnel stops when the command exits.

---

## 6. Docker Compose (optional)

For convenience, here's a `docker-compose.yml`:

```yaml
services:
  flight-card-scanner:
    build: .
    container_name: flight-card-scanner
    restart: unless-stopped
    volumes:
      - /srv/flight-cards:/data
    ports:
      - "127.0.0.1:80:80"
```

Run with:

```bash
docker compose up -d
```

Then configure Funnel as described in step 4.

---

## Troubleshooting

### Container won't start

```bash
docker logs flight-card-scanner
```

Common issues:
- **"Configuration file not found"** — Verify `/srv/flight-cards/config.json`
  exists and the volume mount is correct.
- **"Cannot create image store directory"** — Check directory permissions. The
  container runs as root by default so this is rare with bind mounts.

### Funnel not reachable

1. Confirm Tailscale is running: `tailscale status`
2. Check Funnel status: `tailscale funnel status`
3. Verify DNS propagation (can take up to 10 minutes for new nodes):
   `nslookup myhost.tail1234.ts.net`
4. Ensure the Funnel node attribute is in your tailnet policy file.
   The first `tailscale funnel` command prompts you to enable this.
5. Test the local backend: `curl http://localhost:80/`

### Ollama not reachable from container

If Ollama runs on the Docker host:
- Use `http://host.docker.internal:11434` in `extraction_endpoints`.
- On Linux, you may need `--add-host=host.docker.internal:host-gateway`
  in `docker run`, or add it to `docker-compose.yml`:

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

### Certificate issues

Tailscale Funnel handles certificates automatically. If you hit Let's Encrypt
rate limits (rare), you may need to wait up to 34 hours. This typically only
happens if you repeatedly reset and re-provision certificates.

---

## Security considerations

- The container binds to `127.0.0.1:80` — it is **not** directly accessible
  from the network. All external access goes through Tailscale's encrypted
  tunnel and Funnel relay.
- Tailscale Funnel hides your host's IP address from the public internet.
- Traffic between the Funnel relay and your host is encrypted end-to-end
  (WireGuard). The relay servers cannot decrypt the content.
- Consider restricting Funnel access using your tailnet policy's `nodeAttrs`
  to control which nodes can expose Funnels.
