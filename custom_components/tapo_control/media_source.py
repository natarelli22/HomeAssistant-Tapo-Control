"""
Tapo Media Source - Tapo Care & Local Recordings Provider.
Displays and plays recordings backed up from Tapo Care or local storage.
Integrates seamlessly with Home Assistant Media Browser and Advanced Camera Card.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import shutil
import struct
from urllib.parse import parse_qsl, quote, urlencode, urlparse
from typing import Any

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.models import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

try:
    import zoneinfo
except ImportError:
    zoneinfo = None

try:
    from homeassistant.util import dt as dt_util
except ImportError:
    dt_util = None

try:
    from homeassistant.components.http.auth import async_sign_path
except ImportError:
    async_sign_path = None

try:
    from homeassistant.components.media_source import (
        async_resolve_media as resolve_media_source,
    )
except ImportError:
    resolve_media_source = None

from .const import (
    DOMAIN,
    LOGGER,
    ENABLE_MEDIA_SYNC,
    RECORDINGS_SOURCE,
    RECORDINGS_SOURCE_SD,
    RECORDINGS_SOURCE_TAPO_CARE,
    MEDIA_VIEW_DAYS_ORDER,
    MEDIA_VIEW_RECORDINGS_ORDER,
)
from .utils import (
    getColdDirPathForEntry,
    getHotDirPathForEntry,
    getDataPath,
)


_DURATION_CACHE: dict[str, tuple[float, float, int]] = {}


def get_mp4_duration(filepath: Path | str) -> int | None:
    """Read the duration of an MP4 file in seconds using the mvhd box."""
    path_str = str(filepath)
    try:
        st = os.stat(path_str)
        cached = _DURATION_CACHE.get(path_str)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return cached[2]

        with open(path_str, "rb") as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                size, box_type = struct.unpack(">I4s", header)
                if box_type == b"moov":
                    moov_content_size = size - 8 if size > 0 else 65536
                    sub_bytes = f.read(min(moov_content_size, 32768))
                    mvhd_idx = sub_bytes.find(b"mvhd")
                    if mvhd_idx != -1:
                        v_offset = mvhd_idx + 4
                        version = sub_bytes[v_offset]
                        if version == 0:
                            timescale, duration = struct.unpack(
                                ">II", sub_bytes[v_offset + 12 : v_offset + 20]
                            )
                        elif version == 1:
                            timescale, duration = struct.unpack(
                                ">IQ", sub_bytes[v_offset + 20 : v_offset + 32]
                            )
                        else:
                            return None
                        if timescale > 0:
                            dur_sec = max(1, int(round(duration / timescale)))
                            _DURATION_CACHE[path_str] = (
                                st.st_mtime,
                                st.st_size,
                                dur_sec,
                            )
                            return dur_sec
                    return None
                elif size == 1:
                    ext_size = struct.unpack(">Q", f.read(8))[0]
                    if ext_size < 16:
                        break
                    f.seek(ext_size - 16, 1)
                elif size == 0:
                    break
                elif size > 8:
                    f.seek(size - 8, 1)
                else:
                    break
    except Exception:
        pass
    return None


def get_video_timestamps(
    video_path: Path | str,
    date_str: str | None = None,
    time_zone_str: str | None = None,
) -> tuple[int, int]:
    """Calculate start and end timestamps (Unix epoch seconds) for a video file.

    Compatible with Advanced Camera Card's startDate and endDate filters.
    """
    p = Path(video_path)
    stem = p.stem

    # 1. SD card format: start_ts-end_ts (e.g. 1726097713-1726097740.mp4)
    unix_match = re.search(r"(\d{10})-(\d{10})", stem)
    if unix_match:
        try:
            s_ts = int(unix_match.group(1))
            e_ts = int(unix_match.group(2))
            if e_ts <= s_ts:
                e_ts = s_ts + 30
            return s_ts, e_ts
        except Exception:
            pass

    # Resolve timezone
    tz = None
    if time_zone_str:
        if zoneinfo:
            try:
                tz = zoneinfo.ZoneInfo(time_zone_str)
            except Exception:
                pass
        if tz is None and dt_util:
            try:
                tz = dt_util.get_time_zone(time_zone_str)
            except Exception:
                pass
    if tz is None:
        try:
            tz = datetime.now().astimezone().tzinfo
        except Exception:
            tz = timezone.utc

    duration_sec = get_mp4_duration(p) or 30

    # 2. Full datetime patterns: YYYY-MM-DD-HH-MM-SS or YYYY-MM-DD_HH-MM-SS
    # Ex: 2026-09-11_23-35-13_0_1268dab1c2.mp4
    full_matches = list(
        re.finditer(
            r"(\d{4})[-_](\d{2})[-_](\d{2})[-_]+(\d{2})[-_](\d{2})[-_](\d{2})",
            stem,
        )
    )
    if full_matches:
        try:
            y, mo, d, h, mi, s = map(int, full_matches[0].groups())
            dt_start = datetime(y, mo, d, h, mi, s, tzinfo=tz)
            start_ts = int(dt_start.timestamp())

            if len(full_matches) > 1:
                y2, mo2, d2, h2, mi2, s2 = map(int, full_matches[1].groups())
                dt_end = datetime(y2, mo2, d2, h2, mi2, s2, tzinfo=tz)
                end_ts = int(dt_end.timestamp())
                if end_ts > start_ts:
                    return start_ts, end_ts

            return start_ts, start_ts + max(1, duration_sec)
        except Exception:
            pass

    # 3. Time in filename (HH-MM-SS) with explicit hour validation (00-23)
    time_matches = list(re.finditer(r"(?<!\d)([01]\d|2[0-3])[-_]([0-5]\d)[-_]([0-5]\d)(?!\d)", stem))
    if time_matches and date_str:
        date_match = re.search(r"(\d{4})[-_](\d{2})[-_](\d{2})", date_str)
        if date_match:
            try:
                y, mo, d = map(int, date_match.groups())
                h, mi, s = map(int, time_matches[0].groups())
                dt_start = datetime(y, mo, d, h, mi, s, tzinfo=tz)
                start_ts = int(dt_start.timestamp())

                if len(time_matches) > 1:
                    h2, mi2, s2 = map(int, time_matches[1].groups())
                    dt_end = datetime(y, mo, d, h2, mi2, s2, tzinfo=tz)
                    end_ts = int(dt_end.timestamp())
                    if end_ts > start_ts:
                        return start_ts, end_ts

                return start_ts, start_ts + max(1, duration_sec)
            except Exception:
                pass

    # 4. Fallback to file mtime
    try:
        if p.exists():
            st_mtime = int(p.stat().st_mtime)
            return max(0, st_mtime - duration_sec), st_mtime
    except Exception:
        pass

    now_ts = int(datetime.now(timezone.utc).timestamp())
    return now_ts, now_ts + duration_sec


def format_video_title(video_path: Path | str) -> str:
    """Extract a user-friendly 'start - end' time from the video file.

    Examples:
      2026-09-11_23-35-13_0_1268dab1c2.mp4 (duration 30s) -> 23:35:13 - 23:35:43
      1726097713-1726097740.mp4 -> 23:35:13 - 23:35:40
    """
    p = Path(video_path)
    stem = p.stem

    # 1. Unix timestamp format
    unix_match = re.search(r"(\d{10})-(\d{10})", stem)
    if unix_match:
        try:
            dt_start = datetime.fromtimestamp(int(unix_match.group(1)))
            dt_end = datetime.fromtimestamp(int(unix_match.group(2)))
            return f"{dt_start.strftime('%H:%M:%S')} - {dt_end.strftime('%H:%M:%S')}"
        except Exception:
            pass

    duration_sec = get_mp4_duration(p) or 30

    # 2. Full datetime patterns (matches YYYY-MM-DD-HH-MM-SS or YYYY-MM-DD_HH-MM-SS anywhere in stem)
    full_matches = list(
        re.finditer(
            r"(\d{4})[-_](\d{2})[-_](\d{2})[-_]+(\d{2})[-_](\d{2})[-_](\d{2})",
            stem,
        )
    )
    if full_matches:
        try:
            h1, m1, s1 = (
                int(full_matches[0].group(4)),
                int(full_matches[0].group(5)),
                int(full_matches[0].group(6)),
            )
            if 0 <= h1 < 24 and 0 <= m1 < 60 and 0 <= s1 < 60:
                if len(full_matches) > 1:
                    h2, m2, s2 = (
                        int(full_matches[1].group(4)),
                        int(full_matches[1].group(5)),
                        int(full_matches[1].group(6)),
                    )
                    if (h2, m2, s2) != (h1, m1, s1) and (0 <= h2 < 24 and 0 <= m2 < 60 and 0 <= s2 < 60):
                        return f"{h1:02d}:{m1:02d}:{s1:02d} - {h2:02d}:{m2:02d}:{s2:02d}"

                total_start_sec = h1 * 3600 + m1 * 60 + s1
                total_end_sec = (total_start_sec + duration_sec) % 86400
                end_h = total_end_sec // 3600
                end_m = (total_end_sec % 3600) // 60
                end_s = total_end_sec % 60
                return f"{h1:02d}:{m1:02d}:{s1:02d} - {end_h:02d}:{end_m:02d}:{end_s:02d}"
        except Exception:
            pass

    # 3. Time pattern (HH-MM-SS) with explicit hour validation (00-23)
    time_matches = list(
        re.finditer(r"(?<!\d)([01]\d|2[0-3])[-_]([0-5]\d)[-_]([0-5]\d)(?!\d)", stem)
    )
    if time_matches:
        try:
            h1 = int(time_matches[0].group(1))
            m1 = int(time_matches[0].group(2))
            s1 = int(time_matches[0].group(3))

            if len(time_matches) > 1:
                h2 = int(time_matches[1].group(1))
                m2 = int(time_matches[1].group(2))
                s2 = int(time_matches[1].group(3))
                if (h2, m2, s2) != (h1, m1, s1):
                    return f"{h1:02d}:{m1:02d}:{s1:02d} - {h2:02d}:{m2:02d}:{s2:02d}"

            total_start_sec = h1 * 3600 + m1 * 60 + s1
            total_end_sec = (total_start_sec + duration_sec) % 86400
            end_h = total_end_sec // 3600
            end_m = (total_end_sec % 3600) // 60
            end_s = total_end_sec % 60
            return f"{h1:02d}:{m1:02d}:{s1:02d} - {end_h:02d}:{end_m:02d}:{end_s:02d}"
        except Exception:
            pass

    return stem


async def async_get_media_source(hass: HomeAssistant) -> TapoMediaSource:
    """Set up Tapo media source."""
    LOGGER.debug("async_get_media_source (Tapo dynamic / Cold Storage driven)")
    entries = hass.config_entries.async_entries(DOMAIN)
    entry = entries[0] if entries else None
    return TapoMediaSource(hass, entry)


def build_identifier(
    params: dict[str, Any] | None = None, base: str = DOMAIN
) -> str:
    """Construct a clean media-source identifier URL with encoded query params."""
    if params is None:
        return f"{base}"
    clean = {k: v for k, v in params.items() if v is not None}
    query = urlencode(clean, doseq=True)
    return f"{base}/?{query}"


def parse_identifier(identifier: str) -> dict[str, str]:
    """Parse query params from an identifier regardless of URL path shape."""
    query = urlparse(identifier).query
    return dict(parse_qsl(query, keep_blank_values=True))


class TapoMediaSource(MediaSource):
    """Provide Tapo recordings as media source without hardcoded paths."""

    name = "Tapo: Recordings"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry | None) -> None:
        """Initialize TapoMediaSource."""
        super().__init__(DOMAIN)
        self.hass = hass
        self.entry = entry

    def generate_view(
        self,
        identifier: str,
        title: str,
        can_play: bool,
        can_expand: bool,
        thumbnail: str | None = None,
        children: list[BrowseMediaSource] | None = None,
    ) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY if can_expand else MediaClass.VIDEO,
            media_content_type=MediaType.VIDEO,
            title=title,
            can_play=can_play,
            can_expand=can_expand,
            thumbnail=thumbnail,
            children_media_class=MediaClass.DIRECTORY if can_expand else None,
            children=children,
        )

    def _get_thumbnail_url(self, thumb_path: Path | None) -> str | None:
        """Generate a loadable URL for a thumbnail image dynamically."""
        if not thumb_path:
            return None
        try:
            if not thumb_path.exists() or thumb_path.stat().st_size == 0:
                return None
        except OSError:
            return None

        resolved_thumb = thumb_path.resolve()

        # 1. Inspect media directories registered in Home Assistant (e.g. /media)
        media_dirs: dict[str, str] = getattr(self.hass.config, "media_dirs", {}) or {}
        if "local" not in media_dirs and os.path.exists("/media"):
            media_dirs["local"] = "/media"

        for source_dir_id, base_dir in media_dirs.items():
            try:
                base_p = Path(base_dir).resolve()
                if resolved_thumb == base_p or base_p in resolved_thumb.parents:
                    rel_media = str(resolved_thumb.relative_to(base_p)).replace("\\", "/")
                    url = f"/media/{source_dir_id}/{quote(rel_media)}"
                    if async_sign_path:
                        try:
                            return async_sign_path(
                                self.hass, url, expiration=timedelta(days=2)
                            )
                        except Exception:
                            try:
                                return async_sign_path(self.hass, url, 172800)
                            except Exception:
                                pass
                    return url
            except (ValueError, AttributeError, OSError):
                pass

        # 2. Inspect Home Assistant www directory (/config/www -> /local/)
        www_dir = Path(getDataPath()) / "www"
        if hasattr(self.hass.config, "path"):
            try:
                www_dir = Path(self.hass.config.path("www"))
            except Exception:
                pass

        try:
            resolved_www = www_dir.resolve()
            if resolved_thumb == resolved_www or resolved_www in resolved_thumb.parents:
                rel_www = str(resolved_thumb.relative_to(resolved_www)).replace("\\", "/")
                return f"/local/{quote(rel_www)}"
        except (ValueError, AttributeError, OSError):
            pass

        return None

    def _find_thumbnail(self, camera_path: Path, date: str, video_file: Path) -> Path | None:
        """Find thumbnail file in thumbs/ or video folder dynamically."""
        stem = video_file.stem
        thumbs_dir = camera_path / "thumbs"

        candidates = [
            thumbs_dir / date / f"{stem}.jpg",
            thumbs_dir / f"{stem}.jpg",
            video_file.parent / f"{stem}.jpg",
            thumbs_dir / date.replace("_", "-") / f"{stem}.jpg",
            thumbs_dir / date.replace("-", "_") / f"{stem}.jpg",
            thumbs_dir / date / f"{stem}.png",
            thumbs_dir / f"{stem}.png",
            video_file.with_suffix(".jpg"),
            video_file.with_suffix(".png"),
        ]
        for c in candidates:
            try:
                if c.exists() and c.stat().st_size > 0:
                    return c
            except OSError:
                continue

        # Prefix matching fallback em thumbs
        stem_prefix = stem.rsplit("_", 1)[0]
        search_dirs = [
            thumbs_dir / date,
            thumbs_dir / date.replace("_", "-"),
            thumbs_dir / date.replace("-", "_"),
            thumbs_dir,
            video_file.parent,
        ]
        for sdir in search_dirs:
            try:
                if sdir.is_dir():
                    matches = sorted(sdir.glob(f"{stem_prefix}_*.jpg"))
                    if matches and matches[0].stat().st_size > 0:
                        return matches[0]
            except OSError:
                continue

        return None

    def _get_cameras(self) -> dict[str, dict[str, Any]]:
        """Return a mapping of camera key -> {title, path, entry_id, child_id, storage_path}.

        The storage path of each camera is obtained dynamically from Cold storage path
        (via getColdDirPathForEntry) or the default integration path.
        """
        cameras: dict[str, dict[str, Any]] = {}
        ha_entries: dict[str, Any] = self.hass.data.get(DOMAIN, {}) or {}

        for entry_id, entry_data in ha_entries.items():
            name = entry_data.get("name")
            if not name:
                continue

            config_entry = entry_data.get("entry")
            sync_source = (
                config_entry.data.get(
                    RECORDINGS_SOURCE,
                    config_entry.data.get("media_sync_source", RECORDINGS_SOURCE_SD),
                )
                if config_entry
                else RECORDINGS_SOURCE_SD
            )
            is_sync_enabled = entry_data.get(ENABLE_MEDIA_SYNC, False)

            # If Tapo Care is selected, only display in Tapo: Recordings if media_sync is enabled
            if sync_source == RECORDINGS_SOURCE_TAPO_CARE and not is_sync_enabled:
                continue

            safe_key = (
                re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._") or "camera"
            )
            cold_dir = getColdDirPathForEntry(self.hass, entry_id)
            cam_path = Path(cold_dir)

            cameras[safe_key] = {
                "title": name,
                "path": cam_path,
                "entry_id": entry_id,
                "child_id": None,
                "storage_path": str(cam_path),
            }

            if entry_data.get("isParent") and entry_data.get("childDevices"):
                for child in entry_data.get("childDevices", []):
                    c_name = child.get("name")
                    c_id = child.get("camData", {}).get("basic_info", {}).get("dev_id")
                    if not c_name:
                        continue
                    if sync_source == RECORDINGS_SOURCE_TAPO_CARE and not child.get(
                        ENABLE_MEDIA_SYNC, is_sync_enabled
                    ):
                        continue
                    c_safe = (
                        re.sub(r"[^A-Za-z0-9._-]+", "_", c_name.strip()).strip("._")
                        or "child"
                    )
                    cameras[c_safe] = {
                        "title": c_name,
                        "path": cam_path,
                        "entry_id": entry_id,
                        "child_id": c_id,
                        "storage_path": str(cam_path),
                    }

        return cameras

    def _get_camera_dates(self, camera_path: Path) -> dict[str, list[Path]]:
        """Return a mapping of date ('YYYY-MM-DD') -> list of video Paths."""
        if not camera_path.exists():
            return {}

        videos_dir = camera_path / "videos" if (camera_path / "videos").exists() else camera_path
        dates_map: dict[str, list[Path]] = {}

        if not videos_dir.exists() or not videos_dir.is_dir():
            return dates_map

        # Check date subdirectories
        try:
            for sub in videos_dir.iterdir():
                if sub.is_dir():
                    date_match = re.match(r"^\d{4}[-_]\d{2}[-_]\d{2}$", sub.name)
                    if date_match:
                        d_norm = sub.name.replace("_", "-")
                        vids = [f for f in sub.glob("*.mp4") if f.is_file() and f.stat().st_size > 0]
                        if vids:
                            dates_map.setdefault(d_norm, []).extend(vids)
                    elif re.match(r"^\d{8}$", sub.name):
                        d_norm = f"{sub.name[:4]}-{sub.name[4:6]}-{sub.name[6:8]}"
                        vids = [f for f in sub.glob("*.mp4") if f.is_file() and f.stat().st_size > 0]
                        if vids:
                            dates_map.setdefault(d_norm, []).extend(vids)
                    elif len(sub.name) >= 10 and re.match(r"^\d{4}[-_]\d{2}[-_]\d{2}", sub.name):
                        d_norm = sub.name[:10].replace("_", "-")
                        vids = [f for f in sub.glob("*.mp4") if f.is_file() and f.stat().st_size > 0]
                        if vids:
                            dates_map.setdefault(d_norm, []).extend(vids)
        except OSError:
            pass

        # Check flat files in videos_dir
        try:
            for f in videos_dir.glob("*.mp4"):
                if f.is_file() and f.stat().st_size > 0:
                    stem = f.stem
                    m = re.match(r"^(\d{4}[-_]\d{2}[-_]\d{2})", stem)
                    if m:
                        d = m.group(1).replace("_", "-")
                        dates_map.setdefault(d, []).append(f)
                    else:
                        m_unix = re.search(r"(\d{10})-\d{10}", stem)
                        if m_unix:
                            try:
                                d = datetime.fromtimestamp(int(m_unix.group(1))).strftime("%Y-%m-%d")
                                dates_map.setdefault(d, []).append(f)
                            except Exception:
                                dates_map.setdefault("Other", []).append(f)
                        else:
                            dates_map.setdefault("Other", []).append(f)
        except OSError:
            pass

        # Also check date folders directly in camera_path (if camera_path != videos_dir)
        if camera_path != videos_dir and camera_path.exists():
            try:
                for sub in camera_path.iterdir():
                    if sub.is_dir() and sub.name not in {"videos", "thumbs"}:
                        date_match = re.match(r"^\d{4}[-_]\d{2}[-_]\d{2}$", sub.name)
                        if date_match:
                            d_norm = sub.name.replace("_", "-")
                            vids = [f for f in sub.glob("*.mp4") if f.is_file() and f.stat().st_size > 0]
                            if vids:
                                dates_map.setdefault(d_norm, []).extend(vids)
                        elif re.match(r"^\d{8}$", sub.name):
                            d_norm = f"{sub.name[:4]}-{sub.name[4:6]}-{sub.name[6:8]}"
                            vids = [f for f in sub.glob("*.mp4") if f.is_file() and f.stat().st_size > 0]
                            if vids:
                                dates_map.setdefault(d_norm, []).extend(vids)
            except OSError:
                pass

        return dates_map

    async def async_browse_media(
        self,
        item: MediaSourceItem,
    ) -> BrowseMediaSource:
        """Browse media items."""
        if item.identifier is None or item.identifier in (DOMAIN, f"{DOMAIN}/", ""):
            return await self.hass.async_add_executor_job(self._browse_root)

        query = parse_identifier(item.identifier)
        if not query and "/" in item.identifier:
            parts = item.identifier.split("/", 1)
            query = parse_identifier(parts[1])

        camera = query.get("camera")
        entry = query.get("entry")
        date = query.get("date")
        camera_path_str = query.get("camera_path")
        camera_path = Path(camera_path_str) if camera_path_str else None

        if date and (camera or entry):
            return await self.hass.async_add_executor_job(
                self._browse_date_videos, camera or entry, camera_path, date, query
            )
        elif camera or entry:
            return await self.hass.async_add_executor_job(
                self._browse_camera_dates, camera or entry, camera_path, query
            )

        return await self.hass.async_add_executor_job(self._browse_root)

    def _browse_root(self) -> BrowseMediaSource:
        """List all available cameras with their entry ID and storage_path for ACC."""
        camera_children: list[BrowseMediaSource] = []
        cameras = self._get_cameras()

        for cam_key in sorted(cameras.keys()):
            cam_info = cameras[cam_key]
            title = str(cam_info["title"])
            cam_path = str(cam_info["path"])
            entry_id = cam_info.get("entry_id")
            child_id = cam_info.get("child_id")
            storage_path = cam_info.get("storage_path", cam_path)

            params: dict[str, Any] = {
                "camera": cam_key,
                "camera_path": cam_path,
                "title": title,
                "storage_path": storage_path,
            }
            if entry_id:
                params["entry"] = entry_id
            if child_id:
                params["childID"] = child_id

            camera_children.append(
                self.generate_view(
                    identifier=build_identifier(params),
                    title=title,
                    can_play=False,
                    can_expand=True,
                )
            )

        if not camera_children:
            return self.generate_view(
                identifier=build_identifier(),
                title=self.name,
                can_play=False,
                can_expand=True,
                children=[
                    self.generate_view(
                        identifier=build_identifier({"empty": "1"}),
                        title="No cameras found",
                        can_play=False,
                        can_expand=False,
                    )
                ],
            )

        return self.generate_view(
            identifier=build_identifier(),
            title=self.name,
            can_play=False,
            can_expand=True,
            children=camera_children,
        )

    def _format_date_title(self, date_str: str) -> str:
        """Format a date string (YYYY-MM-DD) according to the Home Assistant language/locale."""
        m = re.match(r"^(\d{4})[-_](\d{2})[-_](\d{2})$", date_str)
        if not m:
            return date_str

        year, month, day = m.group(1), m.group(2), m.group(3)
        lang = getattr(getattr(self.hass, "config", None), "language", "en") or "en"
        lang = lang.lower()

        try:
            import babel.dates
            from datetime import date as dt_date
            d_obj = dt_date(int(year), int(month), int(day))
            locale_str = lang.replace("-", "_")
            return babel.dates.format_date(d_obj, format="short", locale=locale_str)
        except Exception:
            pass

        if lang.startswith(("pt", "es", "fr", "it", "nl")) or lang == "en-gb":
            return f"{day}/{month}/{year}"
        elif lang.startswith("de"):
            return f"{day}.{month}.{year}"
        elif lang in ("en", "en-us"):
            return f"{month}/{day}/{year}"
        else:
            return date_str

    def _browse_camera_dates(
        self, camera: str, camera_path: Path | None, query: dict[str, str]
    ) -> BrowseMediaSource:
        """List recording dates for a camera."""
        entry_id = query.get("entry")
        if entry_id and entry_id in self.hass.data.get(DOMAIN, {}):
            entry_data = self.hass.data[DOMAIN][entry_id]
            config_entry = entry_data.get("entry")
            sync_source = (
                config_entry.data.get(
                    RECORDINGS_SOURCE,
                    config_entry.data.get("media_sync_source", RECORDINGS_SOURCE_SD),
                )
                if config_entry
                else RECORDINGS_SOURCE_SD
            )
            if sync_source == RECORDINGS_SOURCE_TAPO_CARE and not entry_data.get(
                ENABLE_MEDIA_SYNC, False
            ):
                return self.generate_view(
                    identifier=build_identifier(query),
                    title=query.get("title", camera),
                    can_play=False,
                    can_expand=True,
                    children=[],
                )

        if not camera_path or not camera_path.exists():
            if entry_id:
                camera_path = Path(getColdDirPathForEntry(self.hass, entry_id))
            else:
                cameras = self._get_cameras()
                if camera in cameras:
                    camera_path = Path(cameras[camera]["path"])

        dates_map = self._get_camera_dates(camera_path) if camera_path else {}

        media_view_days_order = "Descending"
        if self.entry:
            media_view_days_order = self.entry.data.get(
                MEDIA_VIEW_DAYS_ORDER, "Descending"
            )

        sorted_dates = sorted(
            dates_map.keys(),
            reverse=(media_view_days_order == "Descending"),
        )

        date_children: list[BrowseMediaSource] = []
        for d in sorted_dates:
            display_title = self._format_date_title(d)

            date_params = {
                **query,
                "camera": camera,
                "camera_path": str(camera_path) if camera_path else "",
                "date": d,
                "title": display_title,
            }
            date_children.append(
                self.generate_view(
                    identifier=build_identifier(date_params),
                    title=display_title,
                    can_play=False,
                    can_expand=True,
                )
            )

        camera_title = query.get("title", camera)
        return self.generate_view(
            identifier=build_identifier(query),
            title=camera_title,
            can_play=False,
            can_expand=True,
            children=date_children,
        )

    def _browse_date_videos(
        self, camera: str, camera_path: Path | None, date: str, query: dict[str, str]
    ) -> BrowseMediaSource:
        """List video clips for a specific date."""
        entry_id = query.get("entry")
        if entry_id and entry_id in self.hass.data.get(DOMAIN, {}):
            entry_data = self.hass.data[DOMAIN][entry_id]
            config_entry = entry_data.get("entry")
            sync_source = (
                config_entry.data.get(
                    RECORDINGS_SOURCE,
                    config_entry.data.get("media_sync_source", RECORDINGS_SOURCE_SD),
                )
                if config_entry
                else RECORDINGS_SOURCE_SD
            )
            if sync_source == RECORDINGS_SOURCE_TAPO_CARE and not entry_data.get(
                ENABLE_MEDIA_SYNC, False
            ):
                return self.generate_view(
                    identifier=build_identifier(query),
                    title=query.get("title", date),
                    can_play=False,
                    can_expand=True,
                    children=[],
                )

        if not camera_path or not camera_path.exists():
            if entry_id:
                camera_path = Path(getColdDirPathForEntry(self.hass, entry_id))
            else:
                cameras = self._get_cameras()
                if camera in cameras:
                    camera_path = Path(cameras[camera]["path"])

        dates_map = self._get_camera_dates(camera_path) if camera_path else {}
        files = dates_map.get(date, [])

        media_view_recordings_order = "Ascending"
        if self.entry:
            media_view_recordings_order = self.entry.data.get(
                MEDIA_VIEW_RECORDINGS_ORDER, "Ascending"
            )

        files.sort(
            key=lambda x: x.name,
            reverse=(media_view_recordings_order == "Descending"),
        )

        tz_name = getattr(self.hass.config, "time_zone", None) or "UTC"

        video_children: list[BrowseMediaSource] = []
        for f in files:
            title = format_video_title(f)
            thumb_file = self._find_thumbnail(camera_path, date, f) if camera_path else None
            thumb_url = self._get_thumbnail_url(thumb_file) if thumb_file else None
            start_ts, end_ts = get_video_timestamps(f, date, tz_name)

            LOGGER.debug(
                "Tapo media item '%s': start_ts=%s, end_ts=%s, thumb_url=%s",
                f.name,
                start_ts,
                end_ts,
                thumb_url,
            )

            video_params = {
                **query,
                "camera": camera,
                "camera_path": str(camera_path) if camera_path else "",
                "date": date,
                "video_path": str(f),
                "file": f.name,
                "title": title,
                "startDate": str(start_ts),
                "endDate": str(end_ts),
                "duration": str(max(1, end_ts - start_ts)),
            }

            video_children.append(
                self.generate_view(
                    identifier=build_identifier(video_params),
                    title=title,
                    can_play=True,
                    can_expand=False,
                    thumbnail=thumb_url,
                )
            )

        date_title = query.get("title", date)
        return self.generate_view(
            identifier=build_identifier(query),
            title=date_title,
            can_play=False,
            can_expand=True,
            children=video_children,
        )

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a media item to a playable URL dynamically."""
        query = parse_identifier(item.identifier)
        if not query and "/" in item.identifier:
            parts = item.identifier.split("/", 1)
            query = parse_identifier(parts[1])

        video_path_str = query.get("video_path")
        entry_id = query.get("entry")
        camera = query.get("camera")
        date = query.get("date")
        file = query.get("file")

        if video_path_str:
            file_path = Path(video_path_str)
        else:
            cam_path = None
            if entry_id:
                cam_path = Path(getColdDirPathForEntry(self.hass, entry_id))
            elif camera:
                cameras = self._get_cameras()
                if camera in cameras:
                    cam_path = Path(cameras[camera]["path"])

            if cam_path and file:
                file_path = cam_path / "videos" / date / file if date else cam_path / "videos" / file
            else:
                file_path = Path("")

        # If the file is not found at exact path, search recursively inside cam_path
        if not file_path.exists() and file:
            cam_path = None
            if entry_id:
                cam_path = Path(getColdDirPathForEntry(self.hass, entry_id))
            elif camera:
                cameras = self._get_cameras()
                if camera in cameras:
                    cam_path = Path(cameras[camera]["path"])

            if cam_path and cam_path.exists():
                matches = list(cam_path.glob(f"**/{file}"))
                if matches and matches[0].exists():
                    file_path = matches[0]

        if not file_path.exists():
            raise Unresolvable(f"File not found: {file_path}")

        resolved_file = file_path.resolve()

        # 1. Check if located in a registered Home Assistant media directory (e.g. /media)
        media_dirs: dict[str, str] = getattr(self.hass.config, "media_dirs", {}) or {}
        if "local" not in media_dirs and os.path.exists("/media"):
            media_dirs["local"] = "/media"

        for source_dir_id, base_dir in media_dirs.items():
            try:
                base_p = Path(base_dir).resolve()
                if resolved_file == base_p or base_p in resolved_file.parents:
                    rel_media = str(resolved_file.relative_to(base_p)).replace("\\", "/")
                    if resolve_media_source:
                        try:
                            return await resolve_media_source(
                                self.hass,
                                f"media-source://media_source/{source_dir_id}/{rel_media}",
                                None,
                            )
                        except Exception as err:
                            LOGGER.debug("Error resolving via media_source (%s)", err)

                    if async_sign_path:
                        try:
                            signed_url = async_sign_path(
                                self.hass,
                                f"/media/{source_dir_id}/{quote(rel_media)}",
                                expiration=timedelta(days=2),
                            )
                            return PlayMedia(signed_url, "video/mp4")
                        except Exception:
                            pass

                    return PlayMedia(f"/media/{source_dir_id}/{quote(rel_media)}", "video/mp4")
            except (ValueError, AttributeError, OSError):
                pass

        # 2. Check if file is located in the www directory (/config/www -> /local/)
        www_dir = Path(getDataPath()) / "www"
        if hasattr(self.hass.config, "path"):
            try:
                www_dir = Path(self.hass.config.path("www"))
            except Exception:
                pass

        try:
            resolved_www = www_dir.resolve()
            if resolved_file == resolved_www or resolved_www in resolved_file.parents:
                rel_www = str(resolved_file.relative_to(resolved_www)).replace("\\", "/")
                return PlayMedia(f"/local/{quote(rel_www)}", "video/mp4")
        except (ValueError, AttributeError, OSError):
            pass

        # 3. If in default cold storage (.storage), copy to hot storage in www/ to serve via HTTP
        if entry_id:
            try:
                hot_dir = Path(getHotDirPathForEntry(self.hass, entry_id))
                hot_file = hot_dir / "videos" / resolved_file.name
                if not hot_file.exists():
                    (hot_dir / "videos").mkdir(parents=True, exist_ok=True)
                    await self.hass.async_add_executor_job(shutil.copyfile, resolved_file, hot_file)
                rel_hot = str(hot_file.resolve().relative_to(resolved_www)).replace("\\", "/")
                return PlayMedia(f"/local/{quote(rel_hot)}", "video/mp4")
            except Exception as e:
                LOGGER.error("Error preparing file in hot storage: %s", e)

        return PlayMedia(f"/local/{quote(resolved_file.name)}", "video/mp4")
