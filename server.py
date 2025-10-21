#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import signal
import sys
import traceback
from typing import Optional

from aiohttp import web

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstWebRTC, GLib
from gi.repository import GstSdp  # for SDP parsing

Gst.init(None)


def log_ex(ex: Exception, prefix: str = ""):
    print(f"{prefix}{ex}", file=sys.stderr)
    traceback.print_exc()


def link_chain(elements):
    for a, b in zip(elements, elements[1:]):
        ok = a.link(b)
        if not ok:
            a_name = f"{a.get_name()}({a.get_factory().get_name() if a.get_factory() else 'unknown'})"
            b_name = f"{b.get_name()}({b.get_factory().get_name() if b.get_factory() else 'unknown'})"
            raise RuntimeError(f"Failed to link {a_name} -> {b_name}")


class WebRTCCamera:
    def __init__(
        self,
        ws,
        loop: asyncio.AbstractEventLoop,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        stun_server: Optional[str],
        source: str,
        v4l2_dev: Optional[str],
        force_sw: bool,
    ):
        self.ws = ws
        self.loop = loop
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.stun_server = stun_server
        self.source = source
        self.v4l2_dev = v4l2_dev
        self.force_sw = force_sw

        self.pipeline: Optional[Gst.Pipeline] = None
        self.webrtc: Optional[Gst.Element] = None

    def build_pipeline(self) -> None:
        pipeline = Gst.Pipeline.new("webrtc-pipeline")

        # Preflight: libnice
        if Gst.ElementFactory.find("nicesrc") is None or Gst.ElementFactory.find("nicesink") is None:
            raise RuntimeError("Missing libnice elements (nicesrc/nicesink). Install: sudo apt-get install -y gstreamer1.0-nice libnice10")

        # Source
        if self.source == "libcamera":
            src = Gst.ElementFactory.make("libcamerasrc", "src")
            if not src:
                raise RuntimeError("Missing plugin 'libcamerasrc'. Install gstreamer1.0-libcamera.")
        elif self.source == "v4l2":
            src = Gst.ElementFactory.make("v4l2src", "src")
            if not src:
                raise RuntimeError("Missing plugin 'v4l2src'. Install gstreamer1.0-plugins-good.")
            if self.v4l2_dev:
                src.set_property("device", self.v4l2_dev)
        elif self.source == "test":
            src = Gst.ElementFactory.make("videotestsrc", "src")
            src.set_property("is-live", True)
            src.set_property("pattern", 0)
        else:
            raise RuntimeError(f"Unknown source: {self.source}")

        # Caps WxH@FPS
        caps_src = Gst.ElementFactory.make("capsfilter", "caps_src")
        caps_src.set_property("caps", Gst.Caps.from_string(
            f"video/x-raw,width={self.width},height={self.height},framerate={self.fps}/1"
        ))

        vconv = Gst.ElementFactory.make("videoconvert", "vconv")
        vscale = Gst.ElementFactory.make("videoscale", "vscale")

        # Encoder
        enc = None
        using_hw = False
        if not self.force_sw:
            enc = Gst.ElementFactory.make("v4l2h264enc", "h264enc")
            if enc:
                using_hw = True
                try:
                    if self.bitrate > 0:
                        enc.set_property("bitrate", int(self.bitrate))  # bps
                except Exception:
                    pass

        if enc is None:
            enc = Gst.ElementFactory.make("x264enc", "h264enc")
            if not enc:
                raise RuntimeError("Missing H.264 encoder. Install gstreamer1.0-plugins-ugly (x264enc) or provide v4l2h264enc.")
            if self.bitrate > 0:
                enc.set_property("bitrate", max(1, self.bitrate // 1000))  # kbps
            enc.set_property("tune", "zerolatency")
            enc.set_property("speed-preset", "ultrafast")
            enc.set_property("key-int-max", int(max(1, self.fps * 2)))

        caps_pre_enc = Gst.ElementFactory.make("capsfilter", "caps_pre_enc")
        if using_hw:
            caps_pre_enc.set_property("caps", Gst.Caps.from_string("video/x-raw,format=NV12"))
        else:
            caps_pre_enc.set_property("caps", Gst.Caps.from_string("video/x-raw,format=I420"))

        queue_enc = Gst.ElementFactory.make("queue", "qenc")

        h264parse = Gst.ElementFactory.make("h264parse", "h264parse")
        try:
            h264parse.set_property("config-interval", 1)
        except Exception:
            pass

        pay = Gst.ElementFactory.make("rtph264pay", "pay0")
        pay.set_property("pt", 96)
        try:
            pay.set_property("config-interval", 1)
        except Exception:
            pass

        webrtcbin = Gst.ElementFactory.make("webrtcbin", "webrtcbin")
        if not webrtcbin:
            raise RuntimeError("Missing plugin 'webrtcbin'. Install gstreamer1.0-plugins-bad.")
        if self.stun_server:
            webrtcbin.set_property("stun-server", self.stun_server)

        webrtcbin.connect("on-negotiation-needed", self._on_negotiation_needed)
        webrtcbin.connect("on-ice-candidate", self._on_ice_candidate)

        for el in [src, caps_src, vconv, vscale, caps_pre_enc, queue_enc, enc, h264parse, pay, webrtcbin]:
            pipeline.add(el)

        link_chain([src, caps_src, vconv, vscale, caps_pre_enc, queue_enc, enc, h264parse, pay])

        # Link payloader to webrtcbin
        pay_src_pad = pay.get_static_pad("src")
        if not pay_src_pad:
            raise RuntimeError("Failed to get payloader src pad")

        if hasattr(webrtcbin, "request_pad_simple"):
            webrtc_sink_pad = webrtcbin.request_pad_simple("sink_%u")
        else:
            webrtc_sink_pad = webrtcbin.get_request_pad("sink_%u")

        if not webrtc_sink_pad:
            raise RuntimeError("Failed to get webrtcbin sink pad")

        if pay_src_pad.link(webrtc_sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Failed to link payloader to webrtcbin")

        self.pipeline = pipeline
        self.webrtc = webrtcbin

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        print(f"[GStreamer] Pipeline built. Source={self.source} | Encoder={'v4l2h264enc (HW)' if using_hw else 'x264enc (SW)'} | pre-enc caps={caps_pre_enc.get_property('caps').to_string()}")

    def start(self) -> None:
        if not self.pipeline:
            self.build_pipeline()
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to set pipeline to PLAYING")
        print("[GStreamer] Pipeline PLAYING")

    def stop(self) -> None:
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            print("[GStreamer] Pipeline stopped")
            self.pipeline = None
            self.webrtc = None

    def _send_ws(self, obj: dict) -> None:
        async def _send():
            if not self.ws.closed:
                await self.ws.send_json(obj)
        # Force: send event loop running
        asyncio.run_coroutine_threadsafe(_send(), self.loop)

    def _create_and_send_offer(self, element: Gst.Element) -> None:
        def on_offer_created(promise: Gst.Promise, _, __):
            try:
                promise.wait()
                reply = promise.get_reply()
                offer = reply.get_value("offer")
                element.emit("set-local-description", offer, None)
                text = offer.sdp.as_text()
                self._send_ws({"type": "offer", "sdp": text})
                print("[WebRTC] Offer created and sent")
            except Exception as ex:
                log_ex(ex, "[WebRTC] Offer error: ")
                self._send_ws({"type": "error", "message": f"Offer error: {ex}"})

        print("[WebRTC] Creating offer...")
        promise = Gst.Promise.new_with_change_func(on_offer_created, None, None)
        element.emit("create-offer", None, promise)

    def _on_negotiation_needed(self, element: Gst.Element) -> None:
        print("[WebRTC] Negotiation needed")
        self._create_and_send_offer(element)

    def _on_ice_candidate(self, element: Gst.Element, mlineindex: int, candidate: str) -> None:
        self._send_ws({"type": "ice", "ice": {"candidate": candidate, "sdpMLineIndex": mlineindex}})

    def renegotiate(self) -> None:
        if self.webrtc:
            self._create_and_send_offer(self.webrtc)

    def handle_sdp_answer(self, sdp_text: str) -> None:
        if not self.webrtc:
            return
        res, sdpmsg = GstSdp.sdp_message_new_from_text(sdp_text)
        if res != GstSdp.SDPResult.OK:
            raise RuntimeError("Failed to parse SDP answer")
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        self.webrtc.emit("set-remote-description", answer, None)
        print("[WebRTC] Remote SDP answer set")

    def handle_ice_candidate(self, candidate: str, mlineindex: int) -> None:
        if self.webrtc:
            self.webrtc.emit("add-ice-candidate", mlineindex, candidate)

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"[GStreamer] ERROR: {err} debug: {dbg}", file=sys.stderr)
            self._send_ws({"type": "error", "message": f"GStreamer ERROR: {err}"})
        elif t == Gst.MessageType.WARNING:
            err, dbg = message.parse_warning()
            print(f"[GStreamer] WARNING: {err} debug: {dbg}")
        elif t == Gst.MessageType.EOS:
            print("[GStreamer] EOS")
        return True


class AppServer:
    def __init__(
        self,
        host: str,
        port: int,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        stun: Optional[str],
        source: str,
        v4l2_dev: Optional[str],
        force_sw: bool,
    ):
        self.host = host
        self.port = port
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.stun = stun
        self.source = source
        self.v4l2_dev = v4l2_dev
        self.force_sw = force_sw

        self.app = web.Application()
        self.app.add_routes([
            web.get("/", self.handle_index),
            web.get("/ws", self.handle_ws),
            web.static("/static", "web"),
        ])

    async def handle_index(self, request: web.Request):
        return web.FileResponse(path="web/index.html")

    async def handle_ws(self, request: web.Request):
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)

        print("[WS] Client connected")
        await ws.send_json({"type": "hello", "message": "ws-ready"})

        # Get loop running
        loop = asyncio.get_running_loop()

        camera = WebRTCCamera(
            ws, loop,
            self.width, self.height, self.fps, self.bitrate,
            self.stun, self.source, self.v4l2_dev, self.force_sw
        )

        try:
            camera.start()
        except Exception as e:
            log_ex(e, "[Server] Camera start failed: ")
            await ws.send_json({"type": "error", "message": f"Camera start failed: {e}"})

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue
                    mtype = data.get("type")
                    if mtype == "answer":
                        try:
                            sdp = data.get("sdp", "")
                            camera.handle_sdp_answer(sdp)
                        except Exception as ex:
                            log_ex(ex, "[Server] SDP answer error: ")
                            await ws.send_json({"type": "error", "message": f"SDP answer error: {ex}"})
                    elif mtype == "ice":
                        ice = data.get("ice", {})
                        candidate = ice.get("candidate")
                        sdpMLineIndex = ice.get("sdpMLineIndex", 0)
                        if candidate:
                            camera.handle_ice_candidate(candidate, int(sdpMLineIndex))
                    elif mtype == "ping":
                        await ws.send_json({"type": "pong"})
                    elif mtype == "ready":
                        camera.renegotiate()
                elif msg.type == web.WSMsgType.ERROR:
                    print(f"[WS] Connection closed with exception: {ws.exception()}")
        finally:
            camera.stop()
            print("[WS] Client disconnected")
        return ws

    def run(self):
        web.run_app(self.app, host=self.host, port=self.port)


def parse_args():
    parser = argparse.ArgumentParser(description="Raspberry Pi WebRTC camera server (Debian 12, libcamera, GStreamer)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8082, help="HTTP/WebSocket port (default 8082)")
    parser.add_argument("--width", type=int, default=1280, help="Video width (default 1280)")
    parser.add_argument("--height", type=int, default=720, help="Video height (default 720)")
    parser.add_argument("--fps", type=int, default=30, help="Frames per second (default 30)")
    parser.add_argument("--bitrate", type=int, default=2_500_000, help="Target bitrate in bps (default 2,500,000)")
    parser.add_argument("--stun", default=os.environ.get("STUN_SERVER", ""), help="STUN server, e.g. stun://stun.l.google.com:19302")
    parser.add_argument("--source", choices=["libcamera", "v4l2", "test"], default=os.environ.get("VIDEO_SOURCE", "libcamera"),
                        help="Video source: libcamera (default), v4l2 (/dev/videoX), test (videotestsrc)")
    parser.add_argument("--v4l2-dev", default=os.environ.get("V4L2_DEVICE", ""), help="V4L2 device path when --source v4l2, e.g. /dev/video0")
    parser.add_argument("--force-sw", action="store_true", help="Force software encoder (x264enc)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    stun = args.stun if args.stun else None
    v4l2_dev = args.v4l2_dev if args.v4l2_dev else None
    server = AppServer(
        args.host, args.port, args.width, args.height, args.fps, args.bitrate, stun,
        args.source, v4l2_dev, args.force_sw
    )
    server.run()