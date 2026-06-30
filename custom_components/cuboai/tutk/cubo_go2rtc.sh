#!/usr/bin/env bash
# cubo_go2rtc.sh — CuboAI -> go2rtc exec source.
#
# go2rtc launches this script as the producer for the `cubo` stream. It runs the
# pure-Python client, which writes one MPEG-TS (HEVC video + interleaved AAC audio
# by default) to stdout for go2rtc to consume.
#
# The defaults are the production stack (MPEG-TS + FRAMEINFO strip + selective-repeat
# loss recovery + clean-GOP, pure-Python backend, immediate start), so no flags are
# needed beyond credentials.
#   * Add  --raw                  for the byte-identical Annex-B passthrough.
#   * Add  --output-format annexb for Annex-B with the FRAMEINFO trailer stripped.
# Audio (AAC) is muxed into the TS by default. Set CUBOAI_MUX_AUDIO=0 for video-only.
#
# `exec` keeps go2rtc's killsignal teardown clean.
#
# Supply your own device credentials and camera IP via the environment (or edit the
# defaults below). cd to wherever you installed these files.
cd "$(dirname "$0")"
exec python3 cuboai_stream_video.py \
  --uid "${CUBO_UID:-YOUR_UID}" \
  --account "${CUBO_ACCOUNT:-admin@YOUR_ACCOUNT}" \
  --password "${CUBO_PASSWORD:-YOUR_PASSWORD}" \
  --camera-ip "${CUBO_CAMERA_IP:-YOUR_CAMERA_IP}"
