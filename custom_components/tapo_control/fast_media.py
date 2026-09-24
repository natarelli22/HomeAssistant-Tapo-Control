"""fast_media.py -- High-speed SD-card recording download and snapshot protocol (port 8800).

Original research, reverse-engineering, and protocol implementation by freeKC:
https://github.com/freeKC/tapo-v4-protocol (MIT License - Copyright (c) 2026 freeKC).

Adapted for HomeAssistant-Tapo-Control.
Uses the official Tapo app's 'download' method on TCP port 8800 (instead of 1x
real-time 'playback'), delivering recordings at maximum link speed (10x-20x realtime),
with A/V sync via X-Data-PTS wall clock headers and native SD card snapshots.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from typing import Callable, Iterator

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

LOGGER = logging.getLogger(__name__)

CLIENT_BOUNDARY = b"--client-stream-boundary--"
DEFAULT_DEVICE_BOUNDARY = b"--device-stream-boundary--"
TS_PACKET = 188
PLAYER_ID = uuid.uuid4().hex.upper()


class FastMediaError(Exception):
    """Raised when media streaming or authentication fails."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _parse_headers(block: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in block.split(b"\r\n"):
        if b":" in line:
            k, v = line.split(b":", 1)
            out[k.strip().decode("latin-1").lower()] = v.strip().decode("latin-1")
    return out


def _parse_kv(s: str, sep: str) -> dict[str, str]:
    """'a="1", b=2' -> {"a": "1", "b": "2"} (values may be quoted)."""
    out: dict[str, str] = {}
    for item in s.split(sep):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def _pes_pts(p: bytes) -> int:
    return (((p[0] >> 1) & 7) << 30) | (p[1] << 22) | ((p[2] >> 1) << 15) | (p[3] << 7) | (p[4] >> 1)


class ClipDemuxer:
    """Demuxes aligned TS packets into video TS and raw G.711 A-law audio.

    Calculates audio_offset using the camera's X-Data-PTS wall clock header,
    avoiding the PTS desynchronization observed on Tapo recordings.
    """

    def __init__(self, on_video: Callable[[bytes], None], on_audio: Callable[[bytes], None]):
        self.on_video = on_video
        self.on_audio = on_audio
        self._rest = b""
        self._kind: dict[int, str] = {}  # pid -> "v" | "a"
        self.first_video_pts: int | None = None
        self.last_video_pts: int | None = None
        self.first_audio_pts: int | None = None
        self.first_video_wall: int | None = None
        self.first_audio_wall: int | None = None
        self.audio_bytes = 0

    @property
    def video_seconds(self) -> float:
        if self.first_video_pts is None or self.last_video_pts is None:
            return 0.0
        return ((self.last_video_pts - self.first_video_pts) & 0x1FFFFFFFF) / 90000.0

    @property
    def audio_offset(self) -> float:
        """Offset in seconds for ffmpeg -itsoffset. 0.0 if not available or implausible."""
        if self.first_audio_wall is None or self.first_video_wall is None:
            return 0.0
        d = (self.first_audio_wall - self.first_video_wall) / 1000.0
        return d if -0.5 <= d <= 1.0 else 0.0

    def feed(self, data: bytes, wall_ms: int | None = None):
        if self._rest:
            data, self._rest = self._rest + data, b""
        video = bytearray()
        n = len(data) - len(data) % TS_PACKET
        for off in range(0, n, TS_PACKET):
            pk = data[off : off + TS_PACKET]
            if pk[0] != 0x47:
                continue
            pid = ((pk[1] & 0x1F) << 8) | pk[2]
            pusi = pk[1] & 0x40
            afc = (pk[3] >> 4) & 3
            pay = 4 + (1 + pk[4] if afc & 2 else 0)
            payload = pk[pay:] if (afc & 1 and pay < TS_PACKET) else b""
            if pusi and payload[:3] == b"\x00\x00\x01":
                stream_id = payload[3]
                kind = "a" if 0xC0 <= stream_id <= 0xDF else "v" if 0xE0 <= stream_id <= 0xEF else None
                if kind:
                    self._kind[pid] = kind
                has_pts = len(payload) >= 14 and payload[7] & 0x80
                if kind == "v" and has_pts:
                    pts = _pes_pts(payload[9:14])
                    if self.first_video_pts is None:
                        self.first_video_pts, self.first_video_wall = pts, wall_ms
                    self.last_video_pts = pts
                elif kind == "a":
                    if has_pts and self.first_audio_pts is None:
                        self.first_audio_pts, self.first_audio_wall = _pes_pts(payload[9:14]), wall_ms
                    payload = payload[9 + payload[8] :]  # strip PES header
            if self._kind.get(pid) == "a":
                if payload:
                    self.audio_bytes += len(payload)
                    self.on_audio(bytes(payload))
            else:
                video += pk
        self._rest = data[n:]
        if video:
            self.on_video(bytes(video))


class MediaSession:
    """Authenticated HTTP-like multipart connection to Tapo media port (8800)."""

    def __init__(
        self,
        host: str,
        cloud_password: str,
        port: int = 8800,
        username: str = "admin",
        window: int = 50,
        timeout: float = 15.0,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.cloud_password = cloud_password
        self.window = window
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b""
        self._boundary = DEFAULT_DEVICE_BOUNDARY
        self._key: bytes | None = None
        self._iv: bytes | None = None
        self.session_id: str | None = None
        self._ack_every = max(1, window // 2)
        self._seq = 0

    def _read_until(self, marker: bytes) -> bytes:
        while True:
            i = self._buf.find(marker)
            if i >= 0:
                out, self._buf = self._buf[:i], self._buf[i + len(marker) :]
                return out
            if not self.sock:
                raise FastMediaError("Socket is not connected")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise FastMediaError("Connection closed by camera")
            self._buf += chunk

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            if not self.sock:
                raise FastMediaError("Socket is not connected")
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise FastMediaError("Connection closed by camera")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._buf = b""
        except OSError as err:
            raise FastMediaError(f"Could not connect to {self.host}:{self.port} - {err}") from err

    def _send_request_head(self, authorization: str | None):
        head = [
            b"POST /stream HTTP/1.1",
            b"Content-Type: multipart/mixed;boundary=" + CLIENT_BOUNDARY,
            b"Connection: keep-alive",
            b"Content-Length: -1",
        ]
        if authorization:
            head.append(b"Authorization: " + authorization.encode())
        if not self.sock:
            raise FastMediaError("Socket is not connected")
        self.sock.sendall(b"\r\n".join(head) + b"\r\n\r\n")

    def _read_response_head(self) -> tuple[int, dict[str, str]]:
        block = self._read_until(b"\r\n\r\n")
        status_line, _, rest = block.partition(b"\r\n")
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError) as err:
            raise FastMediaError(f"Invalid media response: {status_line[:60]!r}") from err
        return status, _parse_headers(rest)

    def open(self) -> MediaSession:
        if not self.cloud_password:
            raise FastMediaError("Cloud password is empty or not configured")

        self._connect()
        self._send_request_head(None)
        status, hdr = self._read_response_head()
        if status != 401 or "www-authenticate" not in hdr:
            raise FastMediaError(f"Expected auth challenge, received HTTP {status}")
        chal = _parse_kv(hdr["www-authenticate"].split(" ", 1)[1], ",")
        digest = hashlib.sha256 if chal.get("encrypt_type") == "3" else hashlib.md5
        hashed_pwd = digest(self.cloud_password.encode()).hexdigest().upper()

        # Reconnect on 401 response
        self.close()
        self._connect()

        cnonce = "".join(random.choice("0123456789abcdef") for _ in range(24))
        nc, qop, uri = "00000001", "auth", "/stream"
        ha1 = hashlib.md5(f"{self.username}:{chal['realm']}:{hashed_pwd}".encode()).hexdigest()
        ha2 = hashlib.md5(f"POST:{uri}".encode()).hexdigest()
        response = hashlib.md5(
            f"{ha1}:{chal['nonce']}:{nc}:{cnonce}:{qop}:{ha2}".encode()
        ).hexdigest()
        auth = (
            f'Digest username="{self.username}",realm="{chal["realm"]}",uri="{uri}",'
            f'algorithm=MD5,nonce="{chal["nonce"]}",nc={nc},cnonce="{cnonce}",qop={qop},'
            f'response="{response}",opaque="{chal.get("opaque", "")}"'
        )
        self._send_request_head(auth)
        status, hdr = self._read_response_head()
        if status == 401:
            raise FastMediaError(
                "Media authentication rejected (HTTP 401): check Tapo cloud password",
                code=401,
            )
        if status != 200:
            raise FastMediaError(f"Media port returned HTTP {status}", code=status)
        if "key-exchange" not in hdr:
            raise FastMediaError("Key-Exchange header missing in response")

        for piece in hdr.get("content-type", "").split(";"):
            if piece.strip().startswith("boundary="):
                self._boundary = piece.strip()[len("boundary=") :].encode()
        kx = _parse_kv(hdr["key-exchange"], " ")
        nonce, kx_user = kx["nonce"].encode(), kx.get("username", self.username).encode()
        self._key = hashlib.md5(nonce + b":" + hashed_pwd.encode()).digest()
        self._iv = hashlib.md5(kx_user + b":" + nonce).digest()
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _send_part(self, headers: dict[str, str], body: bytes):
        head = (
            CLIENT_BOUNDARY
            + b"\r\n"
            + b"".join(f"{k}: {v}\r\n".encode() for k, v in headers.items())
            + b"\r\n"
        )
        if self.sock:
            self.sock.sendall(b"--" + head + body + b"\r\n")

    def request(self, params: dict, with_session: bool = False):
        self._seq += 1
        body = json.dumps(
            {"type": "request", "seq": self._seq, "params": params},
            separators=(",", ":"),
        ).encode()
        headers = {"X-Data-Window-Size": str(self.window), "Content-Type": "application/json"}
        if with_session and self.session_id is not None:
            headers["X-Session-Id"] = self.session_id
        headers["Content-Length"] = str(len(body))
        self._send_part(headers, body)

    def stop(self):
        if self.sock is None or self.session_id is None:
            return
        try:
            self.request({"stop": "null", "method": "do"}, with_session=True)
        except OSError:
            pass

    def _ack(self, session_id: str, received: int):
        body = b'{"type":"notification","params":{"event_type":"stream_sequence"}}'
        self._send_part(
            {
                "X-Data-Received": str(received),
                "X-Session-Id": session_id,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body,
        )

    def parts(self) -> Iterator[tuple[str, dict[str, str], bytes]]:
        while True:
            self._read_until(self._boundary)
            hdr = _parse_headers(self._read_until(b"\r\n\r\n"))
            data = self._read_exact(int(hdr.get("content-length", "0")))
            if hdr.get("x-if-encrypt", "0").strip() == "1" and data:
                if self._key is None or self._iv is None:
                    raise FastMediaError("Decryption keys not initialized")
                cipher = AES.new(self._key, AES.MODE_CBC, iv=self._iv)
                try:
                    data = unpad(cipher.decrypt(data), 16)
                except ValueError as err:
                    raise FastMediaError("Media decryption failed (incorrect cloud password?)") from err
            sid, seq = hdr.get("x-session-id"), hdr.get("x-data-sequence")
            if sid is not None:
                self.session_id = sid
            if sid is not None and seq is not None and int(seq) and int(seq) % self._ack_every == 0:
                self._ack(sid, int(seq))
            yield hdr.get("content-type", ""), hdr, data


def _control(data: bytes) -> tuple[bool, int | None]:
    try:
        msg = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return False, None
    params = msg.get("params") if isinstance(msg, dict) else None
    if not isinstance(params, dict):
        return False, None
    code = params.get("error_code")
    if msg.get("type") == "response" and code not in (0, None):
        return False, int(code)
    finished = params.get("event_type") == "stream_status" and params.get("status") == "finished"
    return finished, None


def fetch_snapshot(sess: MediaSession, start: int) -> bytes | None:
    """Fetch camera JPEG snapshot for the recording starting at `start`."""
    sess.request(
        {
            "download": {
                "client_id": 1,
                "channels": [0],
                "media_type": 2,
                "start_time": str(int(start)),
                "player_id": PLAYER_ID,
            },
            "method": "get",
        }
    )
    image = None
    for mimetype, _hdr, data in sess.parts():
        if mimetype == "image/jpeg":
            image = data
        elif mimetype == "application/json":
            finished, code = _control(data)
            if finished or code is not None:
                break
    return image


def stream_clip(
    sess: MediaSession,
    start: int,
    end: int,
    demux: ClipDemuxer,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_data: Callable[[], None] | None = None,
    overrun: float = 5.0,
) -> bool:
    """Stream clip at full link speed into demuxer."""
    limit = float(end - start) + overrun
    sess.request(
        {
            "download": {
                "client_id": 1,
                "channels": [0],
                "media_type": 0,
                "start_time": str(int(start)),
                "end_time": str(int(end)),
                "player_id": PLAYER_ID,
            },
            "method": "get",
        }
    )
    for mimetype, _hdr, data in sess.parts():
        if should_stop():
            return False
        if mimetype == "video/mp2t":
            wall = _hdr.get("x-data-pts", "")
            demux.feed(data, int(wall) if wall.isdigit() else None)
            if on_data:
                on_data()
            if demux.video_seconds >= limit:
                return False
        elif mimetype == "application/json":
            finished, code = _control(data)
            if code is not None:
                raise FastMediaError(f"Download rejected by camera (error code {code})", code=code)
            if finished:
                return True
    return False


class FastDownloader:
    """High-level fast downloader orchestrating media streaming, demuxing, and remux."""

    def __init__(
        self,
        host: str,
        cloud_password: str,
        startDate: int,
        endDate: int,
        output_video_path: str,
        output_thumb_path: str | None = None,
        port: int = 8800,
        ffmpeg_bin: str = "ffmpeg",
        audio_format: str = "alaw",
        audio_rate: int = 8000,
    ):
        self.host = host
        self.cloud_password = cloud_password
        self.startDate = int(startDate)
        self.endDate = int(endDate)
        self.output_video_path = output_video_path
        self.output_thumb_path = output_thumb_path
        self.port = port
        self.ffmpeg_bin = ffmpeg_bin
        self.audio_format = audio_format
        self.audio_rate = audio_rate

    def sync_download(self, progress_callback: Callable[[dict | str], None] | None = None) -> dict:
        """Synchronously download clip and remux into MP4. To be called inside executor."""
        segment_length = max(1, self.endDate - self.startDate)
        if progress_callback:
            progress_callback(
                {
                    "currentAction": "Fast Downloading (SD)",
                    "fileName": self.output_video_path,
                    "progress": 0,
                    "total": segment_length,
                }
            )

        with tempfile.TemporaryDirectory(prefix="tapo_fast_") as tmpdir:
            temp_ts = os.path.join(tmpdir, "clip.ts")
            temp_audio = os.path.join(tmpdir, f"clip.{self.audio_format}")

            last_progress_time = 0.0

            with open(temp_ts, "wb") as fv, open(temp_audio, "wb") as fa:
                demux = ClipDemuxer(fv.write, fa.write)

                def _on_progress():
                    nonlocal last_progress_time
                    now = time.time()
                    if now - last_progress_time >= 1.0:
                        last_progress_time = now
                        if progress_callback:
                            progress_callback(
                                {
                                    "currentAction": "Fast Downloading (SD)",
                                    "fileName": self.output_video_path,
                                    "progress": min(segment_length, round(demux.video_seconds, 1)),
                                    "total": segment_length,
                                }
                            )

                with MediaSession(self.host, self.cloud_password, port=self.port) as sess:
                    stream_clip(sess, self.startDate, self.endDate, demux, on_data=_on_progress)
                    sess.stop()

            if not os.path.exists(temp_ts) or os.path.getsize(temp_ts) == 0:
                raise FastMediaError("No video data received from camera")

            # Remux to MP4
            os.makedirs(os.path.dirname(self.output_video_path), exist_ok=True)
            temp_mp4 = self.output_video_path + ".tmp.mp4"

            has_audio = (
                demux.audio_bytes > 0
                and os.path.exists(temp_audio)
                and os.path.getsize(temp_audio) > 0
            )

            cmd = [self.ffmpeg_bin, "-loglevel", "error", "-y", "-i", temp_ts]
            if has_audio:
                cmd.extend(
                    [
                        "-itsoffset",
                        f"{demux.audio_offset:.3f}",
                        "-f",
                        self.audio_format,
                        "-ar",
                        str(self.audio_rate),
                        "-ac",
                        "1",
                        "-i",
                        temp_audio,
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0?",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                    ]
                )
            else:
                cmd.extend(["-c:v", "copy"])

            cmd.extend(["-movflags", "+faststart", temp_mp4])

            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise FastMediaError(f"ffmpeg remux failed: {res.stderr}")

            shutil.move(temp_mp4, self.output_video_path)

            # Try to fetch native thumbnail if target thumb path is requested
            if self.output_thumb_path and not os.path.exists(self.output_thumb_path):
                try:
                    os.makedirs(os.path.dirname(self.output_thumb_path), exist_ok=True)
                    with MediaSession(self.host, self.cloud_password, port=self.port) as thumb_sess:
                        jpg_data = fetch_snapshot(thumb_sess, self.startDate)
                        if jpg_data and len(jpg_data) > 100:
                            with open(self.output_thumb_path, "wb") as f_thumb:
                                f_thumb.write(jpg_data)
                except Exception as thumb_err:
                    LOGGER.debug("Could not fetch native snapshot: %s", thumb_err)

        # Calculate md5
        md5_hash = ""
        try:
            with open(self.output_video_path, "rb") as f_out:
                md5_hash = hashlib.md5(f_out.read()).hexdigest()
        except OSError:
            pass

        if progress_callback:
            progress_callback(
                {
                    "currentAction": "Finished download",
                    "fileName": self.output_video_path,
                    "progress": segment_length,
                    "total": segment_length,
                }
            )

        return {
            "currentAction": "Finished download",
            "fileName": self.output_video_path,
            "progress": segment_length,
            "total": segment_length,
            "md5": md5_hash,
        }

