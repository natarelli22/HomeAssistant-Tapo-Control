"""OSD Visual Timestamp Pre-roll Synchronization for Tapo Control."""
import os
import re
import time
import zoneinfo
import tempfile
import subprocess
from datetime import datetime, timedelta
import numpy as np
from PIL import Image, ImageFilter

from .const import LOGGER, PREROLL_MIN_DIFF_SEC, PREROLL_MAX_DIFF_SEC

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

WINDOWS_2K = [
    ("H1", 475, 505),
    ("H2", 520, 550),
    ("M1", 610, 640),
    ("M2", 655, 685),
    ("S1", 740, 770),
    ("S2", 795, 825),
]

WINDOWS_1080 = [
    ("H1", 400, 422),
    ("H2", 435, 458),
    ("M1", 505, 530),
    ("M2", 545, 570),
    ("S1", 615, 640),
    ("S2", 650, 675),
]

_CACHED_MATRICES = None


def _get_template_matrices():
    global _CACHED_MATRICES
    if _CACHED_MATRICES is not None:
        return _CACHED_MATRICES

    tmpls_2k = {}
    for d in range(10):
        tmpl_path = os.path.join(TEMPLATES_DIR, f"edge_{d}.png")
        im = Image.open(tmpl_path)
        arr = np.array(im).astype(np.float32)
        norm = float(np.linalg.norm(arr))
        tmpls_2k[d] = (arr, norm)

    h_2k, w_2k = tmpls_2k[0][0].shape
    mat_2k = np.zeros((10, h_2k * w_2k), dtype=np.float32)
    for d in range(10):
        t, n = tmpls_2k[d]
        mat_2k[d] = (t / max(n, 1e-6)).ravel()

    # 1080p scaled templates (38px height)
    h_1080 = 38
    w_1080 = int(round(w_2k * (h_1080 / float(h_2k))))
    mat_1080 = np.zeros((10, h_1080 * w_1080), dtype=np.float32)
    for d in range(10):
        tmpl_path = os.path.join(TEMPLATES_DIR, f"edge_{d}.png")
        im = Image.open(tmpl_path)
        scaled = im.resize((w_1080, h_1080), Image.Resampling.BILINEAR)
        arr = np.array(scaled).astype(np.float32)
        norm = float(np.linalg.norm(arr))
        mat_1080[d] = (arr / max(norm, 1e-6)).ravel()

    _CACHED_MATRICES = (mat_2k, h_2k, w_2k, mat_1080, h_1080, w_1080)
    return _CACHED_MATRICES


def read_time_from_crop(
    crop_arr_edges: np.ndarray,
    tmpl_matrix: np.ndarray,
    tmpl_h: int,
    tmpl_w: int,
    windows: list,
    expected_dt: datetime | None = None,
    max_dy: int = 8,
) -> tuple[str, list[float]]:
    """Recognize HH:MM:SS from edge array using BLAS correlation and beam search."""
    digits_candidates = []

    for name, rx0, rx1 in windows:
        patches = []
        for dy in range(0, max_dy):
            for x in range(rx0, rx1):
                sub = crop_arr_edges[dy : dy + tmpl_h, x : x + tmpl_w]
                if sub.shape == (tmpl_h, tmpl_w):
                    patches.append(sub.ravel())
        if not patches:
            digits_candidates.append([(0, 0.0)])
            continue

        p = np.array(patches, dtype=np.float32)
        p_norms = np.linalg.norm(p, axis=1, keepdims=True)
        valid_mask = (p_norms > 1e-3).ravel()
        if not np.any(valid_mask):
            digits_candidates.append([(0, 0.0)])
            continue

        p_norm = p[valid_mask] / p_norms[valid_mask]
        corr = np.matmul(p_norm, tmpl_matrix.T)
        max_per_digit = np.max(corr, axis=0)

        top2 = np.argsort(max_per_digit)[::-1][:2]
        cands = [(int(d), float(max_per_digit[d])) for d in top2]
        digits_candidates.append(cands)

    prim_digits = [c[0][0] for c in digits_candidates]
    prim_scores = [c[0][1] for c in digits_candidates]
    prim_time_str = f"{prim_digits[0]}{prim_digits[1]}:{prim_digits[2]}{prim_digits[3]}:{prim_digits[4]}{prim_digits[5]}"

    if expected_dt is None:
        return prim_time_str, prim_scores

    try:
        prim_t = datetime.strptime(prim_time_str, "%H:%M:%S").time()
        fn_sec = expected_dt.hour * 3600 + expected_dt.minute * 60 + expected_dt.second
        vis_sec = prim_t.hour * 3600 + prim_t.minute * 60 + prim_t.second
        diff = fn_sec - vis_sec
        if diff < -80000:
            diff += 86400
        elif diff > 80000:
            diff -= 86400
        if PREROLL_MIN_DIFF_SEC <= diff <= PREROLL_MAX_DIFF_SEC:
            return prim_time_str, prim_scores
    except Exception:
        pass

    # Alternate hypothesis re-ranking if top 2 scores are close (<= 0.015)
    options_per_pos = []
    for c in digits_candidates:
        opts = [c[0]]
        if len(c) > 1 and (c[0][1] - c[1][1]) <= 0.015:
            opts.append(c[1])
        options_per_pos.append(opts)

    import itertools

    for combo in itertools.product(*options_per_pos):
        d_list = [x[0] for x in combo]
        s_list = [x[1] for x in combo]
        t_str = f"{d_list[0]}{d_list[1]}:{d_list[2]}{d_list[3]}:{d_list[4]}{d_list[5]}"
        try:
            vt = datetime.strptime(t_str, "%H:%M:%S").time()
            fn_sec = expected_dt.hour * 3600 + expected_dt.minute * 60 + expected_dt.second
            vis_sec = vt.hour * 3600 + vt.minute * 60 + vt.second
            diff = fn_sec - vis_sec
            if diff < -80000:
                diff += 86400
            elif diff > 80000:
                diff -= 86400
            if PREROLL_MIN_DIFF_SEC <= diff <= PREROLL_MAX_DIFF_SEC:
                return t_str, s_list
        except Exception:
            continue

    return prim_time_str, prim_scores


def sync_detect_event_preroll(
    video_path: str,
    expected_start_ts: int,
    ffmpeg_bin: str = "ffmpeg",
) -> tuple[int | None, int | None]:
    """Synchronously extract and detect OSD timestamp with Fallback Frame support.

    Returns:
        tuple (visual_start_timestamp, preroll_seconds) or (None, None) if not detected.
    """
    if not os.path.exists(video_path):
        return None, None

    tz_local = datetime.now().astimezone().tzinfo or zoneinfo.ZoneInfo("UTC")
    expected_dt = datetime.fromtimestamp(expected_start_ts, tz_local).replace(tzinfo=None)

    mat_2k, h_2k, w_2k, mat_1080, h_1080, w_1080 = _get_template_matrices()

    def probe_time_offset(seek_str: str, time_offset_sec: int):
        tmp_crop = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_crop.close()
        try:
            cmd = [
                ffmpeg_bin,
                "-y",
                "-ss",
                seek_str,
                "-i",
                video_path,
                "-vframes",
                "1",
                "-filter:v",
                "crop=in_w:70:0:0",
                tmp_crop.name,
                "-loglevel",
                "error",
            ]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if res.returncode != 0 or not os.path.exists(tmp_crop.name) or os.path.getsize(tmp_crop.name) == 0:
                return None

            im_crop = Image.open(tmp_crop.name)
            is_2k = im_crop.width >= 2000

            if is_2k:
                # y=10..60
                sub_area = im_crop.crop((0, 10, min(1000, im_crop.width), 60))
                edges = np.array(sub_area.convert("L").filter(ImageFilter.FIND_EDGES))
                time_str, scores = read_time_from_crop(
                    edges, mat_2k, h_2k, w_2k, WINDOWS_2K, expected_dt=expected_dt, max_dy=8
                )
                min_threshold = 0.70
            else:
                # 1080p, y=0..50
                sub_area = im_crop.crop((0, 0, min(1000, im_crop.width), 50))
                edges = np.array(sub_area.convert("L").filter(ImageFilter.FIND_EDGES))
                time_str, scores = read_time_from_crop(
                    edges, mat_1080, h_1080, w_1080, WINDOWS_1080, expected_dt=expected_dt, max_dy=12
                )
                min_threshold = 0.58

            if min(scores) < min_threshold:
                return None

            vis_t = datetime.strptime(time_str, "%H:%M:%S").time()
            vis_dt_at_seek = expected_dt.replace(
                hour=vis_t.hour, minute=vis_t.minute, second=vis_t.second, microsecond=0
            )
            # Subtract seek offset to get frame 0 start
            true_dt = vis_dt_at_seek - timedelta(seconds=time_offset_sec)
            true_start_ts = int(true_dt.replace(tzinfo=tz_local).timestamp())
            preroll_sec = expected_start_ts - true_start_ts

            if PREROLL_MIN_DIFF_SEC <= preroll_sec <= PREROLL_MAX_DIFF_SEC:
                return true_start_ts, preroll_sec
            return None
        except Exception as err:
            LOGGER.debug("[osd_sync] Error probing %s at %s: %s", video_path, seek_str, err)
            return None
        finally:
            if os.path.exists(tmp_crop.name):
                try:
                    os.remove(tmp_crop.name)
                except Exception:
                    pass

    # 1. Primary probe: 00:00:00 (Frame 0)
    result = probe_time_offset("00:00:00", 0)
    if result is not None:
        return result

    # 2. Fallback Frame probe: 00:00:01 (1.0s into video)
    LOGGER.debug("[osd_sync] Frame 0 did not yield valid OSD for %s. Probing fallback frame at 00:00:01.", video_path)
    result_fallback = probe_time_offset("00:00:01", 1)
    if result_fallback is not None:
        return result_fallback

    return None, None


async def async_detect_event_preroll(
    hass,
    video_path: str,
    expected_start_ts: int,
    ffmpeg_bin: str = "ffmpeg",
) -> tuple[int | None, int | None]:
    """Asynchronously detect event pre-roll using homeassistant executor."""
    return await hass.async_add_executor_job(
        sync_detect_event_preroll, video_path, expected_start_ts, ffmpeg_bin
    )
