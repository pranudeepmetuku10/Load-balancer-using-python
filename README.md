# Load Balancer in Python

A small HTTP reverse-proxy load balancer built with `aiohttp`.

- **Strategy:** round robin over healthy backends
- **Health checks:** active probing of `/health` every 5 seconds
- **Failure handling:** passive demotion on upstream errors, `503` if no backend is healthy

## Files

- `lb.py` - the load balancer (listens on `:8080`)
- `backend.py` - a tiny test backend with a `/toggle` route to flip its health on or off

## Requirements

- Python 3.10+
- `aiohttp`

Install the dependency if you don't already have it:

```bash
pip install aiohttp
```

## How to test

You will need five terminal windows (three backends, one load balancer, one for sending requests).

### 1. Start three backends

```bash
# terminal 1
python backend.py 9001

# terminal 2
python backend.py 9002

# terminal 3
python backend.py 9003
```

Each backend prints its port on startup and serves:

- `GET /`        - returns `hello from :PORT  path=/...`
- `GET /health`  - returns `200 ok` when healthy, `503 down` when toggled off
- `POST /toggle` - flips the health state

### 2. Start the load balancer

```bash
# terminal 4
python lb.py
```

You should see it bind to `0.0.0.0:8080`. The health-check loop starts immediately and logs any backend state changes.

### 3. Verify round robin

```bash
# terminal 5
for i in 1 2 3 4 5 6; do curl -s localhost:8080/; done
```

Expected output (request order cycles through the three backends):

```
hello from :9001  path=/
hello from :9002  path=/
hello from :9003  path=/
hello from :9001  path=/
hello from :9002  path=/
hello from :9003  path=/
```

### 4. Verify active health checks

Toggle one backend off:

```bash
curl -X POST localhost:9002/toggle
```

Within about 5 seconds the LB log should show:

```
backend http://localhost:9002 -> DOWN
```

Send more requests:

```bash
for i in 1 2 3 4; do curl -s localhost:8080/; done
```

Traffic should now alternate between `:9001` and `:9003` only. Toggle `:9002` back on:

```bash
curl -X POST localhost:9002/toggle
```

After the next health probe, the LB log shows `UP` and round robin includes it again.

### 5. Verify the no-healthy-backends path

Toggle all three off:

```bash
curl -X POST localhost:9001/toggle
curl -X POST localhost:9002/toggle
curl -X POST localhost:9003/toggle
```

Wait roughly 5 seconds, then:

```bash
curl -i localhost:8080/
```

Expected:

```
HTTP/1.1 503 Service Unavailable
...
no healthy backends
```

### 6. Verify passive demotion

Kill one backend process directly (Ctrl-C in its terminal) without toggling, then send a request that happens to be routed to it. The LB returns `502 bad gateway` and marks that backend unhealthy so subsequent requests skip it until it comes back.

## Configuration

Edit the constants at the top of `lb.py`:

- `LISTEN_HOST`, `LISTEN_PORT` - where the LB listens
- `BACKENDS` - list of upstream URLs
- `HEALTH_PATH`, `HEALTH_INTERVAL`, `HEALTH_TIMEOUT` - probing behavior
