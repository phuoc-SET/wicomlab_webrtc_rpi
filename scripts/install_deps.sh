#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update

# Core tools
sudo apt-get install -y \
  python3 python3-pip python3-gi python3-aiohttp \
  gstreamer1.0-tools \
  gstreamer1.0-libcamera \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gir1.2-gst-plugins-base-1.0 \
  gir1.2-gst-plugins-bad-1.0 \
  gstreamer1.0-nice libnice10 \
  libcamera-apps

# Optional: verify plugins
echo "== gst-inspect checks =="
gst-inspect-1.0 libcamerasrc | head -n 5 || true
gst-inspect-1.0 webrtcbin | head -n 5 || true
gst-inspect-1.0 v4l2h264enc | head -n 5 || true
gst-inspect-1.0 x264enc | head -n 5 || true
gst-inspect-1.0 nice | head -n 20 || true

echo "Done. You can now run: python3 server.py --host 0.0.0.0 --port 8082"