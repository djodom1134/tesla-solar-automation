# One image, three roles: the signing proxy, the web app, and the collector.
# They share a codebase, a database and a loopback interface, so building them
# once and selecting behaviour with `command:` keeps the three in lockstep --
# a collector running different code from the app that reads its database is a
# failure mode worth designing out.

# ---------------------------------------------------------------- proxy build
FROM golang:1.23-bookworm AS proxy-build

# Pinned to the exact pseudo-version already proven against this car, not
# @latest: this binary signs vehicle commands, and a silent upgrade between a
# working deploy and the next rebuild is not a surprise worth having.
ARG VEHICLE_COMMAND_VERSION=v0.0.0-20260722231406-724d8c85e3c5
RUN CGO_ENABLED=0 go install \
    github.com/teslamotors/vehicle-command/cmd/tesla-http-proxy@${VEHICLE_COMMAND_VERSION}

# ------------------------------------------------------------------- runtime
FROM python:3.13-slim-bookworm

# tzdata is not optional here. The collector buckets history by local day and
# schedules the garage close by local hour (zoneinfo.ZoneInfo), and without the
# system tz database every one of those lookups raises.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# UID 1000 matches the `d` account on the host, so bind-mounted data and keys
# are readable without chowning the host's own files or running as root.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first: they change far less often than the source, so this layer
# survives most rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --from=proxy-build /go/bin/tesla-http-proxy /usr/local/bin/tesla-http-proxy
COPY . .

# Defaults that make the app container-shaped. Every one is overridable, and
# python-dotenv is loaded with override=False, so these beat .env rather than
# being beaten by it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    CAR_DB=/data/car.db \
    TESLA_TOKEN_FILE=/data/.tokens.json \
    TESLA_PROXY_CERT=/keys/tls-cert.pem \
    TESLA_PROXY_URL=https://localhost:4443

USER app

# No CMD on purpose. Every service in docker-compose.yml names its own command,
# and a default here would just be a fourth possibility nobody wants.
