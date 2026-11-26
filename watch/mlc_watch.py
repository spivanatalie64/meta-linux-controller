#!/usr/bin/env python3
"""mlc_watch -- Live eyes for Meta Linux Controller (issue #17).

A capture layer over the X server: while a watch session is open, an
ffmpeg x11grab process (XCB/XShm capture) grabs the display -- the
whole root window or one window -- at a chosen scale and frame rate,
and this module fans the frames out to:

  - a latest-frame slot (GET /watch/frame in the daemon),
  - any number of MJPEG stream consumers (GET /watch/stream),
  - optionally, a rolling segmented recording on disk
    (mp4 segments under <data_dir>/watch/recordings/, bounded to the
    last KEEP_RECORDING_MINUTES minutes).

Capture runs ONLY while a session is open. A session with no stream
consumers and no frame pulls auto-stops after its idle timeout,
unless recording was explicitly switched on (record mode is a
standing instruction, not idleness). Nothing records unless the
session was opened with record=True or recording was switched on
with /watch/record.

The daemon owns authentication and routing; this module owns the
capture process and the frames. Standard library only.
"""

import glob
import os
import subprocess
import threading
import time

DEFAULT_SCALE = 0.5
DEFAULT_FPS = 10
DEFAULT_IDLE_TIMEOUT_S = 120
SEGMENT_SECONDS = 60
KEEP_RECORDING_MINUTES = 30
JPEG_QUALITY = "5"


class WatchError(Exception):
    pass


class WatchManager:
    def __init__(self, data_dir, env):
        self.data_dir = data_dir
        self.env = dict(env)
        self.rec_dir = os.path.join(data_dir, "watch", "recordings")
        self.ffmpeg_log = os.path.join(data_dir, "watch", "ffmpeg.log")
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._session = None  # dict, see open_session
        self._last_stop = None  # {"reason": str, "at": ts, "session_id": id}
        self._janitor_stop = threading.Event()
        self._janitor = threading.Thread(
            target=self._janitor_loop, daemon=True, name="watch-janitor"
        )
        self._janitor.start()

    # ------------------------------------------------------------ utils

    def _xdotool(self, args, timeout=10):
        p = subprocess.run(
            ["xdotool"] + args, env=self.env,
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def _display_size(self):
        # xdotool getdisplaygeometry reports ONE screen (1920x1080) on
        # this dual-monitor XLibre setup; the root window is the union
        # of both outputs. xrandr's "current" size is the true root
        # size, so ask it first.
        try:
            p = subprocess.run(
                ["xrandr"], env=self.env,
                capture_output=True, text=True, timeout=10,
            )
            for line in p.stdout.splitlines():
                if "current" in line:
                    seg = line.split("current", 1)[1].split(",", 1)[0]
                    w, h = seg.replace("x", " ").split()
                    return int(w), int(h)
        except Exception:
            pass
        code, out, _ = self._xdotool(["getdisplaygeometry"])
        if code == 0:
            parts = out.split()
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        return 3840, 1080

    def _window_geometry(self, window_id):
        code, out, err = self._xdotool(
            ["getwindowgeometry", "--shell", str(window_id)]
        )
        if code != 0:
            raise WatchError(f"no such window {window_id}: {err[:120]}")
        geo = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                geo[k.lower()] = v
        try:
            return {
                "id": str(window_id),
                "x": int(geo["x"]), "y": int(geo["y"]),
                "width": int(geo["width"]), "height": int(geo["height"]),
            }
        except (KeyError, ValueError):
            raise WatchError(f"could not read geometry of window {window_id}")

    # ------------------------------------------------------------ session

    def open_session(self, window_id=None, scale=DEFAULT_SCALE, fps=DEFAULT_FPS,
                     record=False, idle_timeout_s=DEFAULT_IDLE_TIMEOUT_S):
        with self._lock:
            if self._session:
                raise WatchError(
                    "session_active: close the current watch session first "
                    f"(id {self._session['id']})"
                )
            scale = min(1.0, max(0.1, float(scale)))
            fps = min(30, max(1, int(fps)))
            idle_timeout_s = max(10, int(idle_timeout_s))
            display = self.env.get("DISPLAY", ":0")
            if window_id:
                geo = self._window_geometry(window_id)
                cap_w, cap_h = geo["width"], geo["height"]
                target = {"kind": "window", **geo}
            else:
                cap_w, cap_h = self._display_size()
                target = {"kind": "root", "width": cap_w, "height": cap_h}
            out_w = max(2, int(cap_w * scale) // 2 * 2)
            out_h = max(2, int(cap_h * scale) // 2 * 2)
            session = {
                "id": int(time.time()),
                "target": target,
                "scale": scale, "fps": fps,
                "cap_w": cap_w, "cap_h": cap_h,
                "out_w": out_w, "out_h": out_h,
                "display": display,
                "record": False,
                "record_explicit": False,
                "idle_timeout_s": idle_timeout_s,
                "started": time.time(),
                "last_access": time.time(),
                "stream_clients": 0,
                "frame_seq": 0,
                "latest": None,       # (jpeg_bytes, seq, ts)
                "ffmpeg": None,
                "ffmpeg_started": None,
                "stop": threading.Event(),
            }
            self._session = session
            try:
                self._start_ffmpeg(session, record=record)
            except Exception:
                self._session = None
                raise
            threading.Thread(
                target=self._idle_loop, args=(session,),
                daemon=True, name="watch-idle",
            ).start()
            return self._public(session)

    def _ffmpeg_cmd(self, session, record):
        display = session["display"]
        fps = str(session["fps"])
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "x11grab", "-framerate", fps]
        if session["target"]["kind"] == "window":
            cmd += ["-window_id", str(session["target"]["id"])]
        cmd += ["-video_size", f"{session['cap_w']}x{session['cap_h']}",
                "-i", display]
        scale_filter = (
            f"scale={session['out_w']}:{session['out_h']}"
            ":flags=fast_bilinear"
        )
        # Output 1: the live frames, MJPEG over stdout.
        cmd += ["-vf", scale_filter, "-c:v", "mjpeg", "-q:v", JPEG_QUALITY,
                "-f", "image2pipe", "pipe:1"]
        # Output 2 (optional): the rolling segmented recording.
        # Fragmented MP4: a segment that is still being written when
        # the session ends stays playable (a plain MP4 loses its moov
        # and the whole segment with it -- seen in testing).
        if record:
            os.makedirs(self.rec_dir, exist_ok=True)
            cmd += ["-vf", scale_filter, "-c:v", "libx264",
                    "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-movflags",
                    "+frag_keyframe+empty_moov+default_base_moof",
                    "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
                    "-reset_timestamps", "1", "-strftime", "1",
                    os.path.join(self.rec_dir, "seg-%Y%m%d-%H%M%S.mp4")]
        return cmd

    def _start_ffmpeg(self, session, record):
        os.makedirs(os.path.dirname(self.ffmpeg_log), exist_ok=True)
        err = open(self.ffmpeg_log, "wb")
        cmd = self._ffmpeg_cmd(session, record)
        p = subprocess.Popen(
            cmd, env=self.env, stdout=subprocess.PIPE, stderr=err,
            start_new_session=True,
        )
        session["ffmpeg"] = p
        session["ffmpeg_started"] = time.time()
        session["record"] = bool(record)
        threading.Thread(
            target=self._reader_loop, args=(session,),
            daemon=True, name="watch-reader",
        ).start()
        # Give ffmpeg a moment; a dead-on-arrival process (bad window,
        # display gone) should fail the open, not haunt it.
        time.sleep(0.6)
        if p.poll() is not None:
            tail = ""
            try:
                with open(self.ffmpeg_log, "r", errors="replace") as f:
                    tail = f.read()[-300:]
            except OSError:
                pass
            raise WatchError(f"capture process exited immediately: {tail}")

    def _reader_loop(self, session):
        # Drains ffmpeg's stdout until EOF. This loop must NOT stop
        # when a close begins: ffmpeg finalizes the recording segment
        # by flushing its last frames through this pipe, and if the
        # drain stops first, ffmpeg wedges on the full pipe and even
        # SIGKILL cannot save the segment (found in testing: closed
        # sessions left 48-byte unplayable segments). EOF -- ffmpeg
        # exiting -- is the natural end of this loop.
        stream = session["ffmpeg"].stdout
        buf = bytearray()
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        buf.clear()
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start > 0:
                            del buf[:start]
                        break
                    frame = bytes(buf[start:end + 2])
                    del buf[:end + 2]
                    with self._cond:
                        session["frame_seq"] += 1
                        session["latest"] = (
                            frame, session["frame_seq"], time.time()
                        )
                        self._cond.notify_all()
        except Exception:
            pass

    def _idle_loop(self, session):
        while not session["stop"].wait(5):
            if self._session is not session:
                return
            if session["record_explicit"]:
                continue
            idle_for = time.time() - session["last_access"]
            if (session["stream_clients"] == 0
                    and idle_for > session["idle_timeout_s"]):
                session["stop_reason"] = (
                    f"idle auto-stop after {int(idle_for)}s with no consumer"
                )
                self.close_session()
                return

    def _janitor_loop(self):
        while not self._janitor_stop.wait(30):
            self._prune_recordings()

    def _prune_recordings(self):
        cutoff = time.time() - KEEP_RECORDING_MINUTES * 60
        try:
            for path in glob.glob(os.path.join(self.rec_dir, "seg-*.mp4")):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.unlink(path)
                except OSError:
                    pass
        except Exception:
            pass

    def close_session(self):
        with self._lock:
            session = self._session
            if not session:
                return {"ok": True, "was_active": False}
            self._session = None
            self._last_stop = {
                "reason": session.get("stop_reason", "closed by request"),
                "at": time.time(),
                "session_id": session["id"],
            }
        # Signal ffmpeg FIRST, while the reader thread is still
        # draining its stdout (see _reader_loop); only then flag stop.
        p = session.get("ffmpeg")
        if p and p.poll() is None:
            self._stop_ffmpeg(p)
        session["stop"].set()
        with self._cond:
            self._cond.notify_all()
        return {"ok": True, "was_active": True,
                "stop_reason": session.get("stop_reason", "closed by request")}

    @staticmethod
    def _stop_ffmpeg(p):
        """Stop ffmpeg gracefully: SIGINT is ffmpeg's documented
        graceful-quit signal (it finalizes the recording segment);
        SIGTERM/SIGKILL are the fallback ladder."""
        import signal as _signal
        for sig, wait_s in ((_signal.SIGINT, 5), (_signal.SIGTERM, 3)):
            try:
                p.send_signal(sig)
                p.wait(timeout=wait_s)
                return
            except Exception:
                if p.poll() is not None:
                    return
        try:
            p.kill()
        except Exception:
            pass

    def set_record(self, on):
        with self._lock:
            session = self._session
            if not session:
                if not on:
                    return {"ok": True, "recording": False,
                            "note": "no session"}
                # Explicit record mode with no watch session: start a
                # record-only session at the defaults.
                self.open_session(record=True)
                self._session["record_explicit"] = True
                return {"ok": True, "recording": True,
                        "mode": "record-only session started"}
            if bool(on) == session["record"] and on:
                session["record_explicit"] = True
                return {"ok": True, "recording": True,
                        "mode": "already recording"}
            if not on and not session["record"]:
                return {"ok": True, "recording": False,
                        "mode": "was not recording"}
            # Toggling the recording output means respawning ffmpeg
            # with/without the segment output (brief frame gap).
            # Same ordering rule as close_session: signal ffmpeg while
            # its reader is still draining, then flag stop.
            p = session.get("ffmpeg")
            if p and p.poll() is None:
                self._stop_ffmpeg(p)
            session["stop"].set()
            session["stop"] = threading.Event()
            self._start_ffmpeg(session, record=bool(on))
            session["record_explicit"] = bool(on)
            return {"ok": True, "recording": bool(on),
                    "mode": "capture restarted with recording "
                            + ("on" if on else "off")}

    # ------------------------------------------------------------ frames

    def get_frame(self):
        session = self._session
        if not session:
            raise WatchError("no_watch_session")
        session["last_access"] = time.time()
        with self._cond:
            latest = session["latest"]
        if latest is None:
            raise WatchError("no_frame_yet")
        return latest  # (jpeg_bytes, seq, ts)

    def wait_frame(self, after_seq, timeout=15):
        """Block until a frame newer than after_seq exists."""
        session = self._session
        if not session:
            return None
        session["last_access"] = time.time()
        with self._cond:
            self._cond.wait_for(
                lambda: (session["latest"] is not None
                         and session["latest"][1] > after_seq)
                        or self._session is not session,
                timeout=timeout,
            )
            if self._session is not session:
                return None
            return session["latest"]

    def stream_open(self):
        session = self._session
        if not session:
            raise WatchError("no_watch_session")
        session["stream_clients"] += 1
        session["last_access"] = time.time()
        return session

    def stream_close(self, session):
        session["stream_clients"] = max(0, session["stream_clients"] - 1)
        session["last_access"] = time.time()

    # ------------------------------------------------------------ status

    def _public(self, session):
        return {
            "session_id": session["id"],
            "target": session["target"]["kind"],
            "window_id": session["target"].get("id"),
            "scale": session["scale"],
            "fps": session["fps"],
            "width": session["out_w"],
            "height": session["out_h"],
            "recording": session["record"],
            "started_s_ago": round(time.time() - session["started"], 1),
        }

    def status(self):
        session = self._session
        segs = sorted(glob.glob(os.path.join(self.rec_dir, "seg-*.mp4")))
        base = {
            "available": True,
            "active": bool(session),
            "recording_segments": len(segs),
            "newest_segment": os.path.basename(segs[-1]) if segs else None,
            "keep_recording_minutes": KEEP_RECORDING_MINUTES,
            "last_stop": self._last_stop,
        }
        if not session:
            return base
        base.update(self._public(session))
        base["recording"] = session["record"]
        base["record_explicit"] = session["record_explicit"]
        base["stream_clients"] = session["stream_clients"]
        base["frame_seq"] = session["frame_seq"]
        base["idle_timeout_s"] = session["idle_timeout_s"]
        base["idle_for_s"] = round(time.time() - session["last_access"], 1)
        p = session.get("ffmpeg")
        base["ffmpeg_pid"] = p.pid if p else None
        if session["latest"]:
            base["last_frame_age_s"] = round(
                time.time() - session["latest"][2], 2
            )
        if session.get("stop_reason"):
            base["stop_reason"] = session["stop_reason"]
        return base
