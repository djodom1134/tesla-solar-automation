#!/usr/bin/env bash
# Build the image on a host whose cooling has failed, by duty-cycling the
# compiler against package temperature.
#
# servy is an i7-4790K that idles at 72-95 C against a 105 C critical trip. On
# 2026-07-29 an all-core Go compile drove it into hardware thermal shutdown
# twice, taking Home Assistant with it. Capping the compile at one job was not
# enough either: from a pre-cooled 71 C, a single-threaded build still reached
# 98 C in 66 seconds. This host cannot compile CONTINUOUSLY at any parallelism.
#
# So it compiles intermittently. The toolchain is SIGSTOPped above PAUSE_C and
# SIGCONTed below RESUME_C, which converts "too hot to build" into "builds
# slowly" -- the CPU only makes heat while it is running. The hysteresis band
# matters: stopping and starting on one threshold would thrash at the boundary.
#
# Go's compiler is ordinary host processes even inside a build container -- same
# kernel -- so pkill/kill reach them. Signalling the compose CLIENT would not:
# BuildKit's work lives in dockerd and would keep heating the CPU unwatched.
#
# ABORT_C remains as a backstop. If the temperature keeps climbing while the
# compiler is already stopped, the heat is not coming from this build, and
# continuing would be gambling with someone's home automation.
set -uo pipefail

cd "$(dirname "$0")"

PAUSE_C=${PAUSE_C:-90}      # stop the compiler at or above this
RESUME_C=${RESUME_C:-78}    # let it run again below this
ABORT_C=${ABORT_C:-99}      # give up entirely (105 C is the hardware trip)
PRECOOL_C=${PRECOOL_C:-76}
ZONE=${ZONE:-/sys/class/thermal/thermal_zone2/temp}   # x86_pkg_temp

: > thermal.log
: > build.log

pkg_c()   { echo $(( $(cat "$ZONE" 2>/dev/null || echo 0) / 1000 )); }
hot_pids() { pgrep -f 'go/pkg/tool|go build|compile -o|link -o' 2>/dev/null; }
say()     { echo "$(date +%H:%M:%S) $*" >> thermal.log; }

say "pre-cool: waiting below ${PRECOOL_C}C (now $(pkg_c)C)"
for _ in $(seq 1 120); do [ "$(pkg_c)" -lt "$PRECOOL_C" ] && break; sleep 2; done

say "start C=$(pkg_c) pause>=${PAUSE_C} resume<${RESUME_C} abort>=${ABORT_C}"
nice -n 19 ionice -c3 docker compose build --progress plain >> build.log 2>&1 &
CLIENT=$!

PEAK=0; PAUSED=0; STALLS=0
while kill -0 "$CLIENT" 2>/dev/null; do
  T=$(pkg_c)
  [ "$T" -gt "$PEAK" ] && PEAK=$T

  if [ "$PAUSED" -eq 0 ] && [ "$T" -ge "$PAUSE_C" ]; then
    for p in $(hot_pids); do kill -STOP "$p" 2>/dev/null; done
    PAUSED=1; STALLS=$((STALLS+1)); say "${T}C pause #$STALLS"
  elif [ "$PAUSED" -eq 1 ] && [ "$T" -lt "$RESUME_C" ]; then
    for p in $(hot_pids); do kill -CONT "$p" 2>/dev/null; done
    PAUSED=0; say "${T}C resume"
  fi

  # Still climbing with the compiler stopped: the heat is not ours.
  if [ "$PAUSED" -eq 1 ] && [ "$T" -ge "$ABORT_C" ]; then
    say "ABORT ${T}C while paused -- heat is not from this build"
    for p in $(hot_pids); do kill -CONT "$p" 2>/dev/null; kill -KILL "$p" 2>/dev/null; done
    kill "$CLIENT" 2>/dev/null
    echo "ABORTED_HOT ${T}" > build.status
    exit 1
  fi
  sleep 2
done

# Never leave a stopped process behind: a SIGSTOPped compiler would hang the
# build forever and look like a wedged daemon.
for p in $(hot_pids); do kill -CONT "$p" 2>/dev/null; done

wait "$CLIENT"; RC=$?
say "done rc=$RC peak=${PEAK}C pauses=$STALLS"
echo "$([ $RC -eq 0 ] && echo OK || echo FAILED) rc=$RC peak=${PEAK}C pauses=$STALLS" > build.status
exit $RC
