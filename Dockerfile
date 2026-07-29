# One image, three roles: the signing proxy, the web app, and the collector.
# They share a codebase, a database and a loopback interface, so building them
# once and selecting behaviour with `command:` keeps the three in lockstep --
# a collector running different code from the app that reads its database is a
# failure mode worth designing out.

# ---------------------------------------------------------------- proxy build
FROM golang:1.23-bookworm AS proxy-build

# Pinned to the exact commit already proven against this car, not @latest:
# this binary signs vehicle commands, and a silent upgrade between a working
# deploy and the next rebuild is not a surprise worth having. The commit is
# the one encoded in the Mac binary's pseudo-version
# v0.0.0-20260722231406-724d8c85e3c5.
ARG VEHICLE_COMMAND_COMMIT=724d8c85e3c5
# Cloned and built rather than `go install pkg@version`, which refuses this
# module outright: vehicle-command's go.mod carries `replace` directives, and
# go install rejects any module whose go.mod would be interpreted differently
# as a dependency than as the main module.
#
# GOMAXPROCS/-p are pinned to BUILD_JOBS, and on this host that is not a
# preference. servy is an i7-4790K that idles at 72-95 C against a 105 C
# critical trip -- a cooling fault, not a load problem -- and an all-core Go
# compile walked it into hardware thermal shutdown twice on 2026-07-29,
# taking Home Assistant down with it. One compile job draws roughly an
# eighth of the package power of eight. Raise this only on a host whose
# cooling is known good.
ARG BUILD_JOBS=1
# Clone and dependency download are I/O-bound and cool; the compile is the
# heat. Kept in separate layers so a thermally aborted attempt still caches
# everything up to the compile, and each retry redoes only the hot part.
RUN git clone https://github.com/teslamotors/vehicle-command.git /src \
 && cd /src && git checkout ${VEHICLE_COMMAND_COMMIT}
RUN cd /src && go mod download
RUN cd /src \
 && CGO_ENABLED=0 GOMAXPROCS=${BUILD_JOBS} \
    go build -p ${BUILD_JOBS} -o /go/bin/tesla-http-proxy ./cmd/tesla-http-proxy

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
