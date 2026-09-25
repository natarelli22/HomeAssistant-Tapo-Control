import asyncio
import datetime
import hashlib
import pathlib
import onvif
import os
import re
import shutil
import socket
import urllib.parse
import uuid
import requests
import base64
import time

from functools import partial
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from pytapo.media_stream.downloader import Downloader
from .fast_media import FastDownloader, FastMediaError
from homeassistant.components.media_source.error import Unresolvable

from haffmpeg.tools import IMAGE_JPEG, ImageFrame
from onvif import ONVIFCamera
from pytapo import Tapo
from yarl import URL
from homeassistant.helpers.network import NoURLAvailableError, get_url

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.ffmpeg import DATA_FFMPEG

try:
    # Home Assistant moved EventManager from `event` to `event_manager` in 2026.5.
    from homeassistant.components.onvif.event_manager import EventManager
except ModuleNotFoundError:
    from homeassistant.components.onvif.event import EventManager
from homeassistant.const import (
    CONF_IP_ADDRESS,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_HOST,
)
from homeassistant.util import slugify, dt as dt_util

from .const import (
    BRAND,
    CONF_TRANSPORT_METHOD,
    CONTROL_PORT,
    DOMAIN_CONFIG,
    ENABLE_MEDIA_SYNC,
    ENABLE_MOTION_SENSOR,
    DOMAIN,
    ENABLE_WEBHOOKS,
    HOT_DIR_DELETE_TIME,
    LOGGER,
    CLOUD_PASSWORD,
    ENABLE_TIME_SYNC,
    CONF_CUSTOM_STREAM_HD,
    CONF_CUSTOM_STREAM_SD,
    CONF_CUSTOM_STREAM_6,
    CONF_CUSTOM_STREAM_7,
    MEDIA_SYNC_COLD_STORAGE_PATH,
    MEDIA_SYNC_HOURS,
    RECORDINGS_SOURCE,
    RECORDINGS_SOURCE_SD,
    RECORDINGS_SOURCE_TAPO_CARE,
    SD_DOWNLOAD_METHOD,
    SD_DOWNLOAD_METHOD_LEGACY,
    SD_DOWNLOAD_METHOD_FAST,
    SUBDIR_EVENTS,
    SUBDIR_CONTINUOUS,
    SD_SHOW_ONLINE_CONTENT,
    TIME_SYNC_DST,
    TIME_SYNC_NDST,
    TPLINK_DOMAIN,
)

UUID = uuid.uuid4().hex
ALARM_CONFIG_TYPES = ("getAlarm", "getAlarmConfig", "getAlertConfig")


def _is_used_by_tplink(hass: HomeAssistant, host: str) -> bool:
    for entry in hass.config_entries.async_entries(
        TPLINK_DOMAIN, include_ignore=False, include_disabled=False
    ):
        if entry.data.get(CONF_HOST) != host:
            continue
        return True
    return False


def isUsingHTTPS(hass):
    try:
        base_url = get_url(hass, prefer_external=False)
    except NoURLAvailableError:
        try:
            base_url = get_url(hass, prefer_external=True)
        except NoURLAvailableError:
            return True
    LOGGER.debug("Detected base_url schema: " + URL(base_url).scheme)
    return URL(base_url).scheme == "https"


def mark_entry_data_for_refresh(hass: HomeAssistant, entry: dict) -> None:
    config_entry = entry.get("entry")
    root_entry = (
        hass.data.get(DOMAIN, {}).get(config_entry.entry_id) if config_entry else None
    )
    if root_entry is None:
        root_entry = entry

    root_entry["lastUpdate"] = 0
    for child in root_entry.get("childDevices", []):
        child["lastUpdate"] = 0


async def async_force_entry_refresh(hass: HomeAssistant, entry: dict) -> None:
    mark_entry_data_for_refresh(hass, entry)
    await entry["coordinator"].async_request_refresh()


def getStreamSource(entry, stream):
    custom_stream_hd = entry.data.get(CONF_CUSTOM_STREAM_HD, "")
    custom_stream_sd = entry.data.get(CONF_CUSTOM_STREAM_SD, "")
    telephoto_custom_stream6 = entry.data.get(CONF_CUSTOM_STREAM_6, "")
    telephoto_custom_stream7 = entry.data.get(CONF_CUSTOM_STREAM_7, "")
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    host = entry.data.get(CONF_IP_ADDRESS)
    if stream == "stream6" and telephoto_custom_stream6:
        return telephoto_custom_stream6
    if stream == "stream7" and telephoto_custom_stream7:
        return telephoto_custom_stream7
    if stream == "stream1" and custom_stream_hd:
        return custom_stream_hd
    if stream == "stream2" and custom_stream_sd:
        return custom_stream_sd
    username = urllib.parse.quote_plus(username)
    password = urllib.parse.quote_plus(password)
    streamURL = f"rtsp://{username}:{password}@{host}:554/{stream}"
    return streamURL


def pytapoLog(msg):
    LOGGER.debug(f"[pytapo] {msg}")


def pytapoWarnLog(msg):
    LOGGER.warning(f"[pytapo] {msg}")


def isKLAP(host, port, timeout=2):
    try:
        url = f"http://{host}:{port}"
        response = requests.get(url, timeout=timeout)
        return "200 OK" in response.text
    except requests.RequestException:
        return False


def registerController(
    host,
    control_port,
    username,
    password,
    password_cloud="",
    super_secret_key="",
    device_id=None,
    is_klap=None,
    hass=None,
):
    selected_transport_method = (
        hass.data.get(DOMAIN_CONFIG, {}).get(CONF_TRANSPORT_METHOD)
        if hass is not None
        else None
    )
    LOGGER.debug(
        f"Creating Tapo controller with transport method {selected_transport_method}."
    )

    return Tapo(
        host,
        username,
        password,
        password_cloud,
        super_secret_key,
        device_id,
        reuseSession=False,
        printDebugInformation=pytapoLog,
        printWarnInformation=pytapoWarnLog,
        retryStok=False,
        controlPort=control_port,
        isKLAP=is_klap,
        hass=hass,
        transportMethod=selected_transport_method,
    )


def isOpen(ip, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((ip, int(port)))
        s.shutdown(2)
        return True
    except Exception:
        return False


def getDataPath():
    return os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    )


def getColdDirPathForEntry(hass: HomeAssistant, entry_id: str):
    # Fast retrieval of path without file IO
    if (
        entry_id in hass.data[DOMAIN]
        and hass.data[DOMAIN][entry_id]["mediaSyncColdDir"] is not False
    ):
        return hass.data[DOMAIN][entry_id]["mediaSyncColdDir"].rstrip("/")

    coldDirPath = os.path.join(getDataPath(), f".storage/{DOMAIN}/{entry_id}/")
    if entry_id in hass.data[DOMAIN]:
        entry: ConfigEntry = hass.data[DOMAIN][entry_id]["entry"]
    else:  # if device is disabled, get entry from HA storage
        entry: ConfigEntry = hass.config_entries.async_get_entry(entry_id)

    media_sync_cold_storage_path = entry.data.get(MEDIA_SYNC_COLD_STORAGE_PATH)

    if not media_sync_cold_storage_path == "":
        coldDirPath = f"{media_sync_cold_storage_path}/"

    if entry_id in hass.data[DOMAIN]:
        pathlib.Path(coldDirPath + "/videos").mkdir(parents=True, exist_ok=True)
        pathlib.Path(coldDirPath + "/thumbs").mkdir(parents=True, exist_ok=True)
        hass.data[DOMAIN][entry_id]["mediaSyncColdDir"] = coldDirPath

    return coldDirPath.rstrip("/")


def getHotDirPathForEntry(hass: HomeAssistant, entry_id: str):
    if hass.data[DOMAIN][entry_id]["mediaSyncHotDir"] is not False:
        return hass.data[DOMAIN][entry_id]["mediaSyncHotDir"].rstrip("/")

    hotDirPath = os.path.join(getDataPath(), f"www/{DOMAIN}/{entry_id}/")

    if entry_id in hass.data[DOMAIN]:
        if hass.data[DOMAIN][entry_id]["mediaSyncHotDir"] is False:
            pathlib.Path(hotDirPath + "/videos").mkdir(parents=True, exist_ok=True)
            pathlib.Path(hotDirPath + "/thumbs").mkdir(parents=True, exist_ok=True)
            hass.data[DOMAIN][entry_id]["mediaSyncHotDir"] = hotDirPath

        hotDirPath = hass.data[DOMAIN][entry_id]["mediaSyncHotDir"]
    return hotDirPath.rstrip("/")


async def getRecordings(hass, entryData, tapoController, date):
    LOGGER.debug("Getting recordings for date " + date + "...")
    childID = ""
    if entryData["isChild"]:
        childID = entryData["camData"]["basic_info"]["dev_id"]
    recordingsForDay = []
    try:
        recordingsForDay = await hass.async_add_executor_job(
            tapoController.getRecordings, date
        )
        if recordingsForDay is not None:
            for recording in recordingsForDay:
                for recordingKey in recording:
                    entryData["mediaScanResult"][
                        ((childID + "-") if childID != "" else "")
                        + str(recording[recordingKey]["startTime"])
                        + "-"
                        + str(recording[recordingKey]["endTime"])
                    ] = True
    except Exception as err:
        if "-71105" in str(err):
            LOGGER.debug(
                f"Received error -71105 when browsing for recordings for day {date}: {err}. Assuming no recordings."
            )
        else:
            raise err
    return recordingsForDay


def getEntryStorageFile(config_entry, child_id):
    return f"tapo_control_{config_entry.entry_id}{child_id}"


# todo: findMedia needs to run periodically
async def findMedia(hass, entryData, entry):
    entry_id = entry.entry_id
    LOGGER.debug("Finding media for " + entryData["name"] + "...")
    entryData["initialMediaScanDone"] = False
    childID = ""
    if entryData["isChild"]:
        childID = entryData["camData"]["basic_info"]["dev_id"]
    tapoController: Tapo = entryData["controller"]

    recordingsList = await hass.async_add_executor_job(tapoController.getRecordingsList)
    mediaScanResult = {}
    for searchResult in recordingsList:
        for key in searchResult:
            LOGGER.debug(f"Getting media for day {searchResult[key]['date']}...")
            recordingsForDay = await getRecordings(
                hass, entryData, tapoController, searchResult[key]["date"]
            )
            LOGGER.debug(
                f"Looping through recordings for day {searchResult[key]['date']}..."
            )
            for recording in recordingsForDay:
                for recordingKey in recording:
                    filePathVideo = getColdFile(
                        hass,
                        entry_id,
                        recording[recordingKey]["startTime"],
                        recording[recordingKey]["endTime"],
                        "videos",
                        childID=childID,
                    )
                    mediaScanResult[
                        ((childID + "-") if childID != "" else "")
                        + str(recording[recordingKey]["startTime"])
                        + "-"
                        + str(recording[recordingKey]["endTime"])
                    ] = True
                    if os.path.exists(filePathVideo):
                        await processDownload(
                            hass,
                            entry_id,
                            entryData,
                            recording[recordingKey]["startTime"],
                            recording[recordingKey]["endTime"],
                        )
    LOGGER.debug("Found media for " + entryData["name"] + ".")
    entryData["mediaScanResult"] = mediaScanResult
    entryData["initialMediaScanDone"] = True

    await mediaCleanup(hass, entry, entryData)


async def processDownload(
    hass,
    entry_id: int,
    entryData: dict,
    startDate: int,
    endDate: int,
    subfolder: str | None = None,
):
    childID = ""
    if entryData["isChild"]:
        childID = entryData["camData"]["basic_info"]["dev_id"]
    filePath = getFileName(startDate, endDate, False, childID=childID)

    coldFilePath = getColdFile(
        hass,
        entry_id,
        startDate,
        endDate,
        "videos",
        childID=childID,
        subfolder=subfolder,
    )

    if not os.path.exists(coldFilePath):
        raise Unresolvable("Failed to get file from cold storage: " + coldFilePath)

    if filePath not in entryData["downloadedStreams"]:
        entryData["downloadedStreams"][filePath] = {
            startDate: startDate,
            endDate: endDate,
        }
    mediaScanName = (
        ((childID + "-") if childID != "" else "") + str(startDate) + "-" + str(endDate)
    )
    if mediaScanName not in entryData["mediaScanResult"]:
        entryData["mediaScanResult"][mediaScanName] = True

    await generateThumb(
        hass, entry_id, startDate, endDate, childID=childID, subfolder=subfolder
    )


async def generateThumb(
    hass,
    entry_id,
    startDate: int,
    endDate: int,
    childID="",
    subfolder: str | None = None,
):
    filePathThumb = getColdFile(
        hass,
        entry_id,
        startDate,
        endDate,
        "thumbs",
        childID=childID,
        subfolder=subfolder,
    )
    if not os.path.exists(filePathThumb):
        filePathVideo = getColdFile(
            hass,
            entry_id,
            startDate,
            endDate,
            "videos",
            childID=childID,
            subfolder=subfolder,
        )
        _ffmpeg = hass.data[DATA_FFMPEG]
        ffmpeg = ImageFrame(_ffmpeg.binary)
        image = await asyncio.shield(
            ffmpeg.get_image(
                filePathVideo,
                output_format=IMAGE_JPEG,
            )
        )
        os.makedirs(os.path.dirname(filePathThumb), exist_ok=True)
        openHandler = await hass.async_add_executor_job(open, filePathThumb, "wb")
        with openHandler as binary_file:
            binary_file.write(image)
    return filePathThumb


# todo: findMedia needs to run periodically because of this function!!!
async def findFilesNoLongerPresentInCamera(
    hass, entry_id, entryData, extension, folder
):
    LOGGER.debug("findFilesNoLongerPresentInCamera")
    childID = ""
    if entryData.get("isChild"):
        childID = entryData.get("camData", {}).get("basic_info", {}).get("dev_id", "")
    device_name = entryData.get("name")
    if not device_name and "camData" in entryData and "basic_info" in entryData["camData"]:
        device_name = entryData["camData"]["basic_info"].get("device_alias")
    if not device_name:
        device_name = entry_id

    expired_files = []
    subdirs_to_check = []
    if entryData.get("initialMediaScanDone") is True:
        LOGGER.debug("findFilesNoLongerPresentInCamera - Initial scanning done.")
        coldDirPath = getColdDirPathForEntry(hass, entry_id)
        scan_path = os.path.join(coldDirPath, folder)
        if os.path.exists(scan_path):
            def _sync_find_sd_files():
                found = []
                subdirs = []
                for root, dirs, files in os.walk(scan_path):
                    for f in files:
                        if not f.endswith(extension):
                            continue
                        filePath = os.path.join(root, f)
                        fileName = f[:-len(extension)]
                        if (
                            (entryData.get("isChild") is False and fileName.count("-") == 1)
                            or (
                                (entryData.get("isChild") is True and fileName.count("-") == 2)
                                and childID in fileName
                            )
                        ) and fileName not in entryData.get("mediaScanResult", []):
                            LOGGER.debug(
                                "[SD Cleanup - %s] Found recording no longer present in camera: %s",
                                device_name,
                                filePath,
                            )
                            found.append((fileName, filePath))
                    if root != scan_path and root != coldDirPath:
                        subdirs.append(root)
                return found, subdirs

            expired_files, subdirs_to_check = await hass.async_add_executor_job(_sync_find_sd_files)
    return expired_files, subdirs_to_check




async def findColdFilesOlderThanMaxSyncTime(
    hass, entry, entryData, extension, folder
):
    childID = ""
    if entryData.get("isChild"):
        childID = entryData.get("camData", {}).get("basic_info", {}).get("dev_id", "")
    entry_id = entry.entry_id
    mediaSyncHours = entry.data.get(MEDIA_SYNC_HOURS)

    device_name = entryData.get("name")
    if not device_name and "camData" in entryData and "basic_info" in entryData["camData"]:
        device_name = entryData["camData"]["basic_info"].get("device_alias")
    if not device_name:
        device_name = entry.title or entry_id

    if mediaSyncHours != "" and mediaSyncHours is not None:
        coldDirPath = getColdDirPathForEntry(hass, entry_id)
        timeCorrection = 0
        try:
            tapoController: Tapo = entryData.get("controller")
            if tapoController:
                timeCorrection = await hass.async_add_executor_job(
                    tapoController.getTimeCorrection
                )
        except Exception:
            timeCorrection = 0

        mediaSyncTime = int(mediaSyncHours) * 60 * 60
        ts = time.time()
        folder_path = os.path.join(coldDirPath, folder)

        sync_source = entry.data.get(
            RECORDINGS_SOURCE,
            entry.data.get("media_sync_source", RECORDINGS_SOURCE_SD),
        )

        try:
            local_now = dt_util.now()
        except Exception:
            local_now = datetime.datetime.now()

        tapo_care_cutoff_date = None
        tapo_care_cutoff_ts = None
        if sync_source == RECORDINGS_SOURCE_TAPO_CARE:
            retention_days = max(1, int(mediaSyncHours) // 24)
            tapo_care_cutoff_date = local_now.date() - datetime.timedelta(days=retention_days)
            today_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            tapo_care_cutoff_ts = int((today_midnight - datetime.timedelta(days=retention_days)).timestamp())

        def _sync_find_cold_files():
            found_files = []
            scan_path = folder_path
            if not os.path.exists(scan_path):
                if (
                    sync_source == RECORDINGS_SOURCE_TAPO_CARE
                    and folder == "videos"
                    and os.path.exists(coldDirPath)
                ):
                    scan_path = coldDirPath
                else:
                    return [], []
            subdirs_to_check = []
            for root, dirs, files in os.walk(scan_path):
                for f in files:
                    if not f.endswith(extension):
                        continue
                    filePath = os.path.join(root, f)
                    fileName = f[: -len(extension)]

                    # 1. SD card format: {startTS}-{endTS} or {childID}-{startTS}-{endTS}
                    splitFileName = fileName.split("-")
                    is_sd_file = (
                        (
                            entryData.get("isChild") is False
                            and fileName.count("-") == 1
                            and splitFileName[0].isdigit()
                            and splitFileName[1].isdigit()
                        )
                        or (
                            entryData.get("isChild") is True
                            and fileName.count("-") == 2
                            and childID in fileName
                            and splitFileName[-1].isdigit()
                        )
                    )
                    file_end_ts = None
                    file_date = None
                    if is_sd_file:
                        try:
                            file_end_ts = int(splitFileName[-1])
                        except ValueError:
                            file_end_ts = None

                    # 2. Tapo Care format: YYYY-MM-DD_HH-MM-SS or YYYY-MM-DD-HH-MM-SS
                    if file_end_ts is None:
                        full_matches = list(
                            re.finditer(
                                r"(\d{4})[-_](\d{2})[-_](\d{2})[-_]+(\d{2})[-_](\d{2})[-_](\d{2})",
                                fileName,
                            )
                        )
                        if full_matches:
                            try:
                                y, mo, d, h, mi, s = map(int, full_matches[0].groups())
                                file_date = datetime.date(y, mo, d)
                                dt_file = datetime.datetime(
                                    y, mo, d, h, mi, s, tzinfo=local_now.tzinfo
                                )
                                file_end_ts = int(dt_file.timestamp())
                            except Exception:
                                file_end_ts = None
                                file_date = None
                        elif sync_source == RECORDINGS_SOURCE_TAPO_CARE:
                            # Fallback: check if parent directory is a date folder (e.g. YYYY-MM-DD)
                            folder_date_match = re.search(
                                r"(\d{4})[-_](\d{2})[-_](\d{2})", os.path.basename(root)
                            )
                            if folder_date_match:
                                try:
                                    y, mo, d = map(int, folder_date_match.groups())
                                    file_date = datetime.date(y, mo, d)
                                    dt_file = datetime.datetime(
                                        y, mo, d, 23, 59, 59, tzinfo=local_now.tzinfo
                                    )
                                    file_end_ts = int(dt_file.timestamp())
                                except Exception:
                                    file_end_ts = None
                                    file_date = None

                    # 3. Fallback to file st_mtime
                    try:
                        last_modified = os.stat(filePath).st_mtime
                    except OSError:
                        continue

                    is_older = False
                    cutoff = int(ts) - (int(mediaSyncTime) + timeCorrection)
                    if sync_source == RECORDINGS_SOURCE_TAPO_CARE:
                        # In Tapo Care mode, retention is aligned to calendar days from midnight
                        # matching tapo-care-backup retention window so files from a day are kept
                        # until that full day has elapsed and exited the backup window.
                        if file_date is not None and tapo_care_cutoff_date is not None:
                            if file_date < tapo_care_cutoff_date:
                                is_older = True
                        elif file_end_ts is not None and tapo_care_cutoff_ts is not None:
                            if file_end_ts < tapo_care_cutoff_ts:
                                is_older = True
                        else:
                            if ts - last_modified > int(mediaSyncTime):
                                is_older = True
                    else:
                        # In SD Card mode, preserve original behavior
                        if file_end_ts is not None:
                            if (file_end_ts < cutoff) and (
                                ts - last_modified > int(mediaSyncTime)
                            ):
                                is_older = True
                        else:
                            if ts - last_modified > int(mediaSyncTime):
                                is_older = True

                    if is_older:
                        LOGGER.debug(
                            "[%s Cleanup - %s] Found expired recording: %s (cutoff: %s)",
                            sync_source,
                            device_name,
                            filePath,
                            tapo_care_cutoff_date
                            if sync_source == RECORDINGS_SOURCE_TAPO_CARE and tapo_care_cutoff_date
                            else f"{mediaSyncTime}s",
                        )
                        found_files.append((fileName, filePath))

                if root != scan_path and root != coldDirPath:
                    subdirs_to_check.append(root)

            return found_files, subdirs_to_check

        return await hass.async_add_executor_job(_sync_find_cold_files)
    return [], []




def resolve_ha_locale(hass) -> str:
    """Resolve a valid babel locale string from Home Assistant configuration."""
    lang = getattr(getattr(hass, "config", None), "language", "en") or "en"
    country = getattr(getattr(hass, "config", None), "country", None)
    tz = getattr(getattr(hass, "config", None), "time_zone", "") or ""

    lang_clean = str(lang).strip().lower()
    country_clean = str(country).strip().upper() if country else ""
    tz_clean = str(tz).strip().lower()

    if "pt" in lang_clean or country_clean == "BR" or "sao_paulo" in tz_clean or "brasilia" in tz_clean:
        return "pt_PT" if country_clean == "PT" else "pt_BR"

    base_lang = lang_clean.split("-")[0].split("_")[0] if lang_clean else "en"
    if country_clean:
        cand = f"{base_lang}_{country_clean}"
        try:
            import babel
            babel.Locale.parse(cand)
            return cand
        except Exception:
            pass
    try:
        import babel
        babel.Locale.parse(base_lang)
        return base_lang
    except Exception:
        return "en"


def warm_up_date_formatter(hass) -> None:
    """Pre-warm date formatting and locale cache inside executor thread."""
    try:
        import babel.dates
        from datetime import date as dt_date

        locale_str = resolve_ha_locale(hass)
        try:
            babel.dates.format_date(dt_date.today(), format="short", locale=locale_str)
        except Exception:
            babel.dates.format_date(dt_date.today(), format="short", locale="en")
    except Exception as err:
        LOGGER.debug("Could not warm up date formatter: %s", err)


def format_date_ha(hass, date_str: str) -> str:
    """Format a date string (YYYY-MM-DD or YYYY_MM_DD or YYYYMMDD) dynamically according to the Home Assistant language/locale."""
    m = re.match(r"^(\d{4})[-_]?(\d{2})[-_]?(\d{2})", date_str)
    if not m:
        return date_str

    year, month, day = m.group(1), m.group(2), m.group(3)
    locale_str = resolve_ha_locale(hass)

    try:
        import babel.dates
        from datetime import date as dt_date

        d_obj = dt_date(int(year), int(month), int(day))
        try:
            return babel.dates.format_date(d_obj, format="short", locale=locale_str)
        except Exception:
            return babel.dates.format_date(d_obj, format="short", locale="en")
    except Exception:
        return f"{year}-{month}-{day}"


def format_cleanup_counts(
    hass, sync_source: str, date_counts: dict[str, int]
) -> str:
    """Format cleanup summary grouped by date in Home Assistant locale date format."""
    lang = getattr(getattr(hass, "config", None), "language", "en") or "en"
    lang = lang.lower()
    is_pt = lang.startswith("pt")

    parts = []
    for d_key in sorted(date_counts.keys()):
        count = date_counts[d_key]
        unit = (
            ("gravação" if count == 1 else "gravações")
            if is_pt
            else ("recording" if count == 1 else "recordings")
        )
        if d_key == "other":
            parts.append(f"{count} {unit}")
        else:
            fmt_d = format_date_ha(hass, d_key)
            parts.append(f"{fmt_d} ({count} {unit})")

    prefix = f"{sync_source} - Cleaned"
    return f"{prefix}: {', '.join(parts)}"


def format_cleanup_summary(
    hass, sync_source: str, unique_recordings: list[str]
) -> str:
    """Format cleanup summary grouped by date from a list of recording stems."""
    date_counts = {}
    for rec in unique_recordings:
        m = re.match(r"^(\d{4})[-_]?(\d{2})[-_]?(\d{2})", rec)
        if m:
            d_key = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            date_counts[d_key] = date_counts.get(d_key, 0) + 1
        else:
            date_counts["other"] = date_counts.get("other", 0) + 1

    return format_cleanup_counts(hass, sync_source, date_counts)


def format_recording_timestamp(hass, rec_name: str) -> str:
    """Format a recording filename into a localized 'Date - HH:MM:SS' string."""
    # 1. Tapo Care pattern: YYYY-MM-DD_HH-MM-SS or YYYY-MM-DD-HH-MM-SS
    m = re.search(
        r"(\d{4})[-_](\d{2})[-_](\d{2})[-_]+(\d{2})[-_](\d{2})[-_](\d{2})", rec_name
    )
    if m:
        y, mo, d, h, mi, s = map(int, m.groups())
        date_formatted = format_date_ha(hass, f"{y:04d}-{mo:02d}-{d:02d}")
        return f"{date_formatted} - {h:02d}:{mi:02d}:{s:02d}"

    # 2. SD card pattern: {startTS}-{endTS} or {childID}-{startTS}-{endTS}
    sd_match = re.search(r"(\d{10})-(\d{10})", rec_name)
    if sd_match:
        try:
            start_ts = int(sd_match.group(1))
            dt_obj = dt_util.as_local(dt_util.utc_from_timestamp(start_ts))
            date_formatted = format_date_ha(hass, dt_obj.strftime("%Y-%m-%d"))
            time_formatted = dt_obj.strftime("%H:%M:%S")
            return f"{date_formatted} - {time_formatted}"
        except Exception:
            pass

    return rec_name


def format_deleted_recordings_list(
    hass, unique_recordings: list[str], max_items: int = 200
) -> list[str]:
    """Format deleted recordings into localized date-time strings, limited to max_items."""
    formatted = [format_recording_timestamp(hass, rec) for rec in unique_recordings]
    # Deduplicate while preserving order (in case mp4 and jpg or duplicates exist)
    deduped = list(dict.fromkeys(formatted))

    total = len(deduped)
    if total <= max_items:
        return deduped

    lang = getattr(getattr(hass, "config", None), "language", "en") or "en"
    is_pt = lang.lower().startswith("pt")

    remaining = total - max_items
    if is_pt:
        unit = "gravação" if remaining == 1 else "gravações"
        more_msg = f"... e mais {remaining} {unit}"
    else:
        unit = "recording" if remaining == 1 else "recordings"
        more_msg = f"... and {remaining} more {unit}"

    return deduped[:max_items] + [more_msg]


async def mediaCleanup(hass, entry, deviceData):
    entry_id = entry.entry_id

    childID = ""
    if deviceData["isChild"]:
        childID = deviceData["camData"]["basic_info"]["dev_id"]

    device_name = deviceData.get("name")
    if not device_name and "camData" in deviceData and "basic_info" in deviceData["camData"]:
        device_name = deviceData["camData"]["basic_info"].get("device_alias")
    if not device_name:
        device_name = entry.title or entry_id

    LOGGER.debug(
        "Initiating media cleanup for entity "
        + entry_id
        + ", child ID:'"
        + childID
        + "'..."
    )

    ts = time.time()
    deviceData["lastMediaCleanup"] = ts
    hotDirPath = getHotDirPathForEntry(hass, entry_id)

    sync_source = entry.data.get(
        RECORDINGS_SOURCE,
        entry.data.get("media_sync_source", RECORDINGS_SOURCE_SD),
    )
    is_tapo_care = sync_source == RECORDINGS_SOURCE_TAPO_CARE

    def _update_sync_sensor():
        for e in deviceData.get("entities", []):
            entity = e.get("entity")
            if entity and getattr(entity, "_name_suffix", "") == "Recordings Synchronization":
                entity.updateTapo(deviceData.get("camData"))
                entity.async_schedule_update_ha_state(True)

    if is_tapo_care:
        deviceData["runningMediaSync"] = True
        _update_sync_sensor()
        if "coordinator" in deviceData:
            await deviceData["coordinator"].async_request_refresh()
        await asyncio.sleep(1)

    try:
        # clean cache files from old HA instance
        LOGGER.debug(
            "Removing cache files from old HA instances for entity "
            + entry_id
            + ", child ID:'"
            + childID
            + "..."
        )

        await deleteFilesNotIncluding(hass, hotDirPath + "/videos/", UUID)
        await deleteFilesNotIncluding(hass, hotDirPath + "/thumbs/", UUID)

        expired_recordings = {}  # fileName -> list of filePaths
        subdirs_to_check = set()

        show_online_content = entry.data.get(SD_SHOW_ONLINE_CONTENT, True)
        entry_download_method = entry.data.get(
            SD_DOWNLOAD_METHOD, SD_DOWNLOAD_METHOD_LEGACY
        )

        if sync_source == RECORDINGS_SOURCE_SD:
            if entry_download_method == SD_DOWNLOAD_METHOD_FAST and not show_online_content:
                LOGGER.debug(
                    "[%s Cleanup - %s] SD sync deletion disabled: deleting files exclusively based on retention hours.",
                    sync_source,
                    device_name,
                )
            else:
                f1, s1 = await findFilesNoLongerPresentInCamera(
                    hass, entry_id, deviceData, ".mp4", "videos"
                )
                f2, s2 = await findFilesNoLongerPresentInCamera(
                    hass, entry_id, deviceData, ".jpg", "thumbs"
                )
                for fn, fp in f1 + f2:
                    expired_recordings.setdefault(fn, []).append(fp)
                subdirs_to_check.update(s1 + s2)

        f_mp4, s_mp4 = await findColdFilesOlderThanMaxSyncTime(
            hass, entry, deviceData, ".mp4", "videos"
        )
        f_jpg, s_jpg = await findColdFilesOlderThanMaxSyncTime(
            hass, entry, deviceData, ".jpg", "thumbs"
        )
        for fn, fp in f_mp4 + f_jpg:
            expired_recordings.setdefault(fn, []).append(fp)
        subdirs_to_check.update(s_mp4 + s_jpg)

        unique_recordings = sorted(list(expired_recordings.keys()))

        try:
            local_today = dt_util.now().date()
        except Exception:
            local_today = datetime.datetime.now().date()

        if deviceData.get("lastDeletedRecordingsDate") != local_today:
            deviceData["lastDeletedRecordingsDate"] = local_today
            deviceData["lastDeletedRecordingsTotal"] = 0

        if not unique_recordings:
            if (
                not deviceData.get("lastCleanupResult")
                or deviceData.get("lastDeletedRecordingsDate") != local_today
                or deviceData.get("lastCleanupResult") == f"{sync_source} - Cleaned: No expired files"
            ):
                deviceData["lastCleanupResult"] = f"{sync_source} - Cleaned: No expired files"
            LOGGER.debug(
                "[%s Cleanup - %s] Finished cleanup: no expired recordings to remove.",
                sync_source,
                device_name,
            )
        else:
            deviceData["lastMediaCleaned"] = time.time()
            deviceData["lastDeletedRecordingsDate"] = local_today
            sync_sensor_entity_id = None
            for e in deviceData.get("entities", []):
                entity = e.get("entity")
                if entity and getattr(entity, "_name_suffix", "") == "Recordings Synchronization":
                    sync_sensor_entity_id = getattr(entity, "entity_id", None)
                    break

            batch_size = 200
            batches = [
                unique_recordings[i : i + batch_size]
                for i in range(0, len(unique_recordings), batch_size)
            ]

            downloaded_streams = deviceData.get("downloadedStreams", {})
            cumulative_date_counts = {}

            for idx, batch in enumerate(batches):
                def _process_batch_cleanup(batch_stems):
                    for stem in batch_stems:
                        downloaded_streams.pop(stem, None)
                        for f_path in expired_recordings.get(stem, []):
                            try:
                                if os.path.exists(f_path):
                                    os.remove(f_path)
                            except OSError as err:
                                LOGGER.error("Error removing %s: %s", f_path, err)

                    batch_date_counts = {}
                    for stem in batch_stems:
                        m = re.match(r"^(\d{4})[-_]?(\d{2})[-_]?(\d{2})", stem)
                        if m:
                            d_key = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                            batch_date_counts[d_key] = batch_date_counts.get(d_key, 0) + 1
                        else:
                            batch_date_counts["other"] = batch_date_counts.get("other", 0) + 1

                    batch_sum = format_cleanup_counts(hass, sync_source, batch_date_counts)
                    return batch_date_counts, batch_sum

                batch_date_counts, batch_summary = (
                    await hass.async_add_executor_job(_process_batch_cleanup, batch)
                )

                for d_k, c in batch_date_counts.items():
                    cumulative_date_counts[d_k] = cumulative_date_counts.get(d_k, 0) + c

                cumulative_summary = format_cleanup_counts(
                    hass, sync_source, cumulative_date_counts
                )

                deviceData["lastDeletedRecordingsTotal"] += len(batch)
                deviceData["lastCleanupResult"] = cumulative_summary

                LOGGER.debug(
                    "[%s Cleanup - %s] Cleaned batch %d/%d (%d items). Batch: %s | Cumulative: %s",
                    sync_source,
                    device_name,
                    idx + 1,
                    len(batches),
                    len(batch),
                    batch_summary,
                    cumulative_summary,
                )

                if sync_sensor_entity_id and hass.services.has_service("logbook", "log"):
                    try:
                        await hass.services.async_call(
                            "logbook",
                            "log",
                            {
                                "name": device_name,
                                "message": batch_summary,
                                "entity_id": sync_sensor_entity_id,
                                "domain": DOMAIN,
                            },
                        )
                    except Exception as log_err:
                        LOGGER.debug("Could not write cleanup message to logbook: %s", log_err)

                _update_sync_sensor()
                if "coordinator" in deviceData:
                    await deviceData["coordinator"].async_request_refresh()

                if idx < len(batches) - 1:
                    await asyncio.sleep(2)

        # Clean empty date subdirectories after all batches are done
        def _clean_subdirs():
            coldDirPath = getColdDirPathForEntry(hass, entry_id)
            for sdir in sorted(subdirs_to_check, reverse=True):
                try:
                    if (
                        sdir != coldDirPath
                        and os.path.isdir(sdir)
                        and not os.listdir(sdir)
                    ):
                        os.rmdir(sdir)
                        LOGGER.debug(
                            "[%s Cleanup - %s] Removed empty directory: %s",
                            sync_source,
                            device_name,
                            sdir,
                        )
                except OSError:
                    pass

        await hass.async_add_executor_job(_clean_subdirs)

        # Delete everything other than HOT_DIR_DELETE_TIME seconds from hot storage
        LOGGER.debug(
            "Deleting hot storage files older than "
            + str(HOT_DIR_DELETE_TIME)
            + " seconds for entity "
            + entry_id
            + ", child ID:'"
            + childID
            + "..."
        )
        await deleteFilesOlderThan(hass, hotDirPath + "/videos/", HOT_DIR_DELETE_TIME)
        await deleteFilesOlderThan(hass, hotDirPath + "/thumbs/", HOT_DIR_DELETE_TIME)
    finally:
        if is_tapo_care:
            deviceData["runningMediaSync"] = False
        _update_sync_sensor()
        if "coordinator" in deviceData:
            await deviceData["coordinator"].async_request_refresh()


async def deleteDir(hass, dirPath):
    if (
        os.path.exists(dirPath)
        and os.path.isdir(dirPath)
        and dirPath != "/"
        and "tapo_control/" in dirPath
    ):
        LOGGER.debug("Deleting folder " + dirPath + "...")
        await hass.async_add_executor_job(shutil.rmtree, dirPath)


async def deleteFilesOlderThan(hass: HomeAssistant, dirPath, deleteOlderThan):
    now = time.time()
    if os.path.exists(dirPath):

        listDirFiles = await hass.async_add_executor_job(os.listdir, dirPath)
        for f in listDirFiles:
            filePath = os.path.join(dirPath, f)
            last_modified = os.stat(filePath).st_mtime
            if now - last_modified > deleteOlderThan:
                LOGGER.debug("[deleteFilesOlderThan] Removing " + filePath + "...")
                os.remove(filePath)


async def deleteFilesNotIncluding(hass: HomeAssistant, dirPath, includingString):
    if os.path.exists(dirPath):
        listDirFiles = await hass.async_add_executor_job(os.listdir, dirPath)
        for f in listDirFiles:
            filePath = os.path.join(dirPath, f)
            if includingString not in filePath:
                LOGGER.debug("[deleteFilesOlderThan] Removing " + filePath + "...")
                os.remove(filePath)


def processDownloadStatus(
    entryData,
    date: str,
    allRecordingsCount: int,
    recordingCount: int = False,
    item_type: str = "recording",
    hass=None,
):
    type_label = "Event" if item_type == "event" else "Recording"
    entry_obj = entryData.get("entry")
    sync_source = RECORDINGS_SOURCE_SD
    if entry_obj and hasattr(entry_obj, "data"):
        sync_source = entry_obj.data.get(RECORDINGS_SOURCE, RECORDINGS_SOURCE_SD)

    def processUpdate(status):
        LOGGER.debug(status)
        if isinstance(status, str):
            entryData["downloadProgress"] = status
        else:
            action = status.get("currentAction", "")
            if action and action != "Finished download":
                date_formatted = format_date_ha(hass, date) if hass else (
                    f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date
                )
                entryData["downloadProgress"] = (
                    f"{sync_source} - Downloading: {date_formatted}"
                    + (
                        f" ({type_label} {recordingCount} / {allRecordingsCount})"
                        if recordingCount is not False
                        else ""
                    )
                )

                if hass:
                    def notify_sync_sensor():
                        for e in entryData.get("entities", []):
                            entity = e.get("entity")
                            if (
                                entity
                                and getattr(entity, "_name_suffix", "")
                                == "Recordings Synchronization"
                            ):
                                entity.updateTapo(entryData.get("camData"))
                                entity.async_schedule_update_ha_state(True)

                    hass.loop.call_soon_threadsafe(notify_sync_sensor)

    return processUpdate


def getFileName(startDate: int, endDate: int, encrypted=False, childID=""):
    if encrypted:
        return hashlib.md5(
            (str(childID) + str(startDate) + str(endDate)).encode()
        ).hexdigest()
    else:
        return (
            ((str(childID) + "-") if childID != "" else "")
            + str(startDate)
            + "-"
            + str(endDate)
        )


def get_recording_subfolder(rec_data: dict | None) -> str:
    """Classify recording into SUBDIR_EVENTS ('events') or SUBDIR_CONTINUOUS ('continuous')."""
    if not rec_data or not isinstance(rec_data, dict):
        return SUBDIR_EVENTS
    vtype = rec_data.get("vedio_type", rec_data.get("video_type", None))
    if vtype is not None:
        try:
            return SUBDIR_CONTINUOUS if int(vtype) == 1 else SUBDIR_EVENTS
        except (ValueError, TypeError):
            pass
    return SUBDIR_EVENTS


def getColdFile(
    hass: HomeAssistant,
    entry_id: str,
    startDate: int,
    endDate: int,
    folder: str,
    childID="",
    subfolder: str | None = None,
):
    coldDirPath = getColdDirPathForEntry(hass, entry_id)
    fileName = getFileName(startDate, endDate, False, childID=childID)

    if folder == "videos":
        extension = ".mp4"
    elif folder == "thumbs":
        extension = ".jpg"
    else:
        raise Unresolvable("Incorrect folder specified: " + folder)

    entry = hass.config_entries.async_get_entry(entry_id) if hass else None
    entry_data = entry.data if entry else {}
    download_method = entry_data.get(SD_DOWNLOAD_METHOD, SD_DOWNLOAD_METHOD_LEGACY)

    # In Fast mode, use subfolder if provided
    if download_method == SD_DOWNLOAD_METHOD_FAST and subfolder:
        sub_path = os.path.join(coldDirPath, folder, subfolder, f"{fileName}{extension}")
        # Retrocompatibility: if file exists in root, return it
        if not os.path.exists(sub_path):
            root_path = os.path.join(coldDirPath, folder, f"{fileName}{extension}")
            if os.path.exists(root_path):
                return root_path
        return sub_path

    # In Legacy mode (or without subfolder), use traditional root path
    root_path = os.path.join(coldDirPath, folder, f"{fileName}{extension}")
    if not os.path.exists(root_path) and subfolder:
        sub_path = os.path.join(coldDirPath, folder, subfolder, f"{fileName}{extension}")
        if os.path.exists(sub_path):
            return sub_path
    return root_path


def reorganize_media_storage(hass: HomeAssistant, entry_id: str) -> None:
    """Reorganize media files between root and subfolders based on download method."""
    entry = hass.config_entries.async_get_entry(entry_id) if hass else None
    if not entry:
        return
    download_method = entry.data.get(SD_DOWNLOAD_METHOD, SD_DOWNLOAD_METHOD_LEGACY)
    cold_dir = getColdDirPathForEntry(hass, entry_id)
    if not cold_dir or not os.path.exists(cold_dir):
        return

    videos_dir = os.path.join(cold_dir, "videos")
    thumbs_dir = os.path.join(cold_dir, "thumbs")

    if download_method == SD_DOWNLOAD_METHOD_FAST:
        # Move root files into events/ or continuous/
        if os.path.exists(videos_dir):
            events_vdir = os.path.join(videos_dir, SUBDIR_EVENTS)
            cont_vdir = os.path.join(videos_dir, SUBDIR_CONTINUOUS)
            events_tdir = os.path.join(thumbs_dir, SUBDIR_EVENTS)
            cont_tdir = os.path.join(thumbs_dir, SUBDIR_CONTINUOUS)

            try:
                for fname in os.listdir(videos_dir):
                    fpath = os.path.join(videos_dir, fname)
                    if os.path.isfile(fpath) and fname.endswith(".mp4"):
                        m = re.search(r"(\d{10})-(\d{10})", fname)
                        is_continuous = False
                        if m:
                            duration = int(m.group(2)) - int(m.group(1))
                            if duration >= 900:  # 15 minutes or longer is continuous
                                is_continuous = True

                        target_vdir = cont_vdir if is_continuous else events_vdir
                        target_tdir = cont_tdir if is_continuous else events_tdir
                        os.makedirs(target_vdir, exist_ok=True)
                        dest_fpath = os.path.join(target_vdir, fname)
                        if not os.path.exists(dest_fpath):
                            shutil.move(fpath, dest_fpath)
                            LOGGER.info("[Storage Migration] Moved %s -> %s", fname, target_vdir)

                        stem = os.path.splitext(fname)[0]
                        thumb_name = f"{stem}.jpg"
                        root_thumb = os.path.join(thumbs_dir, thumb_name)
                        if os.path.exists(root_thumb):
                            os.makedirs(target_tdir, exist_ok=True)
                            dest_thumb = os.path.join(target_tdir, thumb_name)
                            if not os.path.exists(dest_thumb):
                                shutil.move(root_thumb, dest_thumb)
            except Exception as err:
                LOGGER.error("Error during storage migration to Fast mode: %s", err)

    elif download_method == SD_DOWNLOAD_METHOD_LEGACY:
        # Revert files from events/ and continuous/ back to root
        for sdir_name in (SUBDIR_EVENTS, SUBDIR_CONTINUOUS):
            sub_vdir = os.path.join(videos_dir, sdir_name)
            sub_tdir = os.path.join(thumbs_dir, sdir_name)
            if os.path.exists(sub_vdir) and os.path.isdir(sub_vdir):
                try:
                    for fname in os.listdir(sub_vdir):
                        src = os.path.join(sub_vdir, fname)
                        dst = os.path.join(videos_dir, fname)
                        if os.path.isfile(src) and not os.path.exists(dst):
                            shutil.move(src, dst)
                    if not os.listdir(sub_vdir):
                        os.rmdir(sub_vdir)
                except Exception as err:
                    LOGGER.error("Error reverting videos from %s: %s", sub_vdir, err)

            if os.path.exists(sub_tdir) and os.path.isdir(sub_tdir):
                try:
                    for fname in os.listdir(sub_tdir):
                        src = os.path.join(sub_tdir, fname)
                        dst = os.path.join(thumbs_dir, fname)
                        if os.path.isfile(src) and not os.path.exists(dst):
                            shutil.move(src, dst)
                    if not os.listdir(sub_tdir):
                        os.rmdir(sub_tdir)
                except Exception as err:
                    LOGGER.error("Error reverting thumbs from %s: %s", sub_tdir, err)


async def async_reorganize_media_storage(hass: HomeAssistant, entry_id: str) -> None:
    """Async wrapper to run reorganize_media_storage inside executor."""
    await hass.async_add_executor_job(reorganize_media_storage, hass, entry_id)


async def getHotFile(
    hass: HomeAssistant,
    entry_id: str,
    startDate: int,
    endDate: int,
    folder: str,
    childID="",
):
    coldFilePath = getColdFile(
        hass, entry_id, startDate, endDate, folder, childID=childID
    )
    hotDirPath = getHotDirPathForEntry(hass, entry_id)
    extension = pathlib.Path(coldFilePath).suffix
    fileNameEncrypted = getFileName(startDate, endDate, True, childID=childID)
    hotFilePath = f"{hotDirPath}/{folder}/{fileNameEncrypted}{UUID}{extension}"

    if not os.path.exists(hotFilePath):
        if not os.path.exists(coldFilePath):
            raise Unresolvable("Failed to get file from cold storage: " + coldFilePath)
        await hass.async_add_executor_job(shutil.copyfile, coldFilePath, hotFilePath)
    return hotFilePath


async def getWebFile(
    hass: HomeAssistant,
    entry_id: str,
    startDate: int,
    endDate: int,
    folder: str,
    childID="",
):
    hotFilePath = await getHotFile(
        hass, entry_id, startDate, endDate, folder, childID=childID
    )
    fileWebPath = hotFilePath[hotFilePath.index("/www/") + 5 :]  # remove ./www/

    return f"/local/{fileWebPath}"


async def getRecording(
    hass: HomeAssistant,
    tapo: Tapo,
    entry_id: str,
    entryData: dict,
    date: str,
    startDate: int,
    endDate: int,
    recordingCount: int = False,
    totalRecordingCount: int = False,
    subfolder: str | None = None,
    item_type: str = "recording",
) -> str:
    timeCorrection = await hass.async_add_executor_job(tapo.getTimeCorrection)
    startDate = int(startDate)
    endDate = int(endDate)

    childID = ""
    if entryData["isChild"]:
        childID = entryData["camData"]["basic_info"]["dev_id"]

    coldDirPath = getColdDirPathForEntry(hass, entry_id)
    downloadUID = getFileName(startDate, endDate, False, childID=childID)

    coldFilePath = getColdFile(
        hass,
        entry_id,
        startDate,
        endDate,
        "videos",
        childID=childID,
        subfolder=subfolder,
    )
    if not os.path.exists(coldFilePath):
        # this NEEDS to happen otherwise camera does not send data!
        allRecordings = await hass.async_add_executor_job(tapo.getRecordings, date)

        entry = hass.config_entries.async_get_entry(entry_id)
        entry_data_dict = entry.data if entry else {}
        selected_download_method = entry_data_dict.get(
            SD_DOWNLOAD_METHOD, SD_DOWNLOAD_METHOD_LEGACY
        )

        device_name = entryData.get("camData", {}).get("basic_info", {}).get("device_alias")
        if not device_name:
            device_name = getattr(entry, "title", None) or entry_id

        status_callback = processDownloadStatus(
            entryData,
            date,
            (
                len(allRecordings)
                if totalRecordingCount is False
                else totalRecordingCount
            ),
            recordingCount if recordingCount is not False else False,
            item_type=item_type,
            hass=hass,
        )

        entryData["isDownloadingStream"] = True
        downloadedFile = None

        if selected_download_method == SD_DOWNLOAD_METHOD_FAST:
            entryData["sdDownloadMethod"] = "Fast (Download Protocol)"
            thumb_path = getColdFile(
                hass,
                entry_id,
                startDate,
                endDate,
                "thumbs",
                childID=childID,
                subfolder=subfolder,
            )
            cloud_pwd = (
                getattr(tapo, "cloudPassword", "")
                or entry_data_dict.get(CLOUD_PASSWORD)
                or entry_data_dict.get(CONF_PASSWORD)
                or ""
            )
            ffmpeg_binary = "ffmpeg"
            if DATA_FFMPEG in hass.data and hasattr(hass.data[DATA_FFMPEG], "binary"):
                ffmpeg_binary = hass.data[DATA_FFMPEG].binary

            try:
                fast_downloader = FastDownloader(
                    host=tapo.host,
                    cloud_password=cloud_pwd,
                    startDate=startDate,
                    endDate=endDate,
                    output_video_path=coldFilePath,
                    output_thumb_path=thumb_path,
                    port=getattr(tapo, "streamPort", 8800),
                    ffmpeg_bin=ffmpeg_binary,
                )
                LOGGER.debug(
                    "[Fast Download - %s] Starting fast download for %s (%s to %s)",
                    device_name,
                    coldFilePath,
                    startDate,
                    endDate,
                )
                downloadedFile = await hass.async_add_executor_job(
                    fast_downloader.sync_download, status_callback
                )
                entryData["lastDownloadWarning"] = None
            except Exception as err:
                err_msg = str(err)
                if "401" in err_msg or "authentication" in err_msg.lower():
                    warn_desc = (
                        "Falha de autenticação na porta de mídia 8800 (HTTP 401). "
                        "Verifique a 'Cloud Password' nas configurações da câmera."
                    )
                else:
                    warn_desc = f"Erro no download rápido na porta 8800: {err}"
                LOGGER.warning(
                    "[Fast Download - %s] %s Executando fallback para o método legado (Downloader).",
                    device_name,
                    warn_desc,
                )
                entryData["sdDownloadMethod"] = "Legacy (Fallback)"
                entryData["lastDownloadWarning"] = warn_desc

        if downloadedFile is None:
            if entryData.get("sdDownloadMethod") != "Legacy (Fallback)":
                entryData["sdDownloadMethod"] = "Legacy (Playback)"
            downloader = Downloader(
                tapo,
                startDate,
                endDate,
                timeCorrection,
                coldDirPath + "/videos/",
                0,
                None,
                None,
                downloadUID + ".mp4",
            )
            downloadedFile = await downloader.downloadFile(status_callback)

        entryData["isDownloadingStream"] = False
        if downloadedFile.get("currentAction") == "Recording in progress":
            raise Unresolvable("Recording is currently in progress.")

        hass.bus.fire(
            "tapo_control_media_downloaded",
            {
                "entry_id": entry_id,
                "startDate": startDate,
                "endDate": endDate,
                "filePath": coldFilePath,
            },
        )

    await processDownload(
        hass, entry_id, entryData, startDate, endDate, subfolder=subfolder
    )

    return coldFilePath


def areCameraPortsOpened(host, controlPort=443):
    return isOpen(host, int(controlPort)) and isOpen(host, 554) and isOpen(host, 2020)


async def isRtspStreamWorking(
    hass, host, username, password, stream: str | None = None
):
    LOGGER.debug("[isRtspStreamWorking][%s] Testing RTSP stream.", host)
    _ffmpeg = hass.data[DATA_FFMPEG]
    LOGGER.debug("[isRtspStreamWorking][%s] Creating image frame.", host)
    ffmpeg = ImageFrame(_ffmpeg.binary)
    LOGGER.debug("[isRtspStreamWorking][%s] Encoding username and password.", host)
    username = urllib.parse.quote_plus(username)
    password = urllib.parse.quote_plus(password)

    stream_path = stream or "stream1"
    auth = f"{username}:{password}@" if username or password else ""
    streaming_url = f"rtsp://{auth}{host}:554/{stream_path}"

    safe_streaming_url = streaming_url
    if username:
        safe_streaming_url = safe_streaming_url.replace(username, "HIDDEN_USERNAME")
    if password:
        safe_streaming_url = safe_streaming_url.replace(password, "HIDDEN_PASSWORD")

    LOGGER.debug(
        "[isRtspStreamWorking][%s] Getting image from %s.",
        host,
        safe_streaming_url,
    )
    image = await asyncio.shield(
        ffmpeg.get_image(
            streaming_url,
            output_format=IMAGE_JPEG,
        )
    )
    LOGGER.debug(
        "[isRtspStreamWorking][%s] Image data received.",
        host,
    )
    return not image == b""


def result_has_error(result):
    if (
        result is not False
        and "result" in result
        and "responses" in result["result"]
        and any(
            map(
                lambda x: "error_code" not in x or x["error_code"] == 0,
                result["result"]["responses"],
            )
        )
    ):
        return False
    if result is not False and (
        "error_code" not in result or result["error_code"] == 0
    ):
        return False
    else:
        return True


async def initOnvifEvents(hass, host, username, password):
    device = ONVIFCamera(
        host,
        2020,
        username,
        password,
        f"{os.path.dirname(onvif.__file__)}/wsdl/",
        no_cache=True,
    )
    try:
        LOGGER.debug("[initOnvifEvents] Creating onvif connection...")
        await device.update_xaddrs()
        LOGGER.debug("[initOnvifEvents] Connection estabilished.")
        device_mgmt = await device.create_devicemgmt_service()
        LOGGER.debug("[initOnvifEvents] Getting device information...")
        device_info = await device_mgmt.GetDeviceInformation()
        LOGGER.debug("[initOnvifEvents] Got device information.")
        if "Manufacturer" not in device_info:
            raise Exception("Onvif connection has failed.")

        return {"device": device, "device_mgmt": device_mgmt}
    except Exception as e:
        LOGGER.error("[initOnvifEvents] Initiating onvif connection failed.")
        LOGGER.error(e)

    return False


def tryParseInt(value):
    try:
        return int(value)
    except Exception as e:
        LOGGER.debug("Couldnt parse as integer: %s", str(e))
        return None


def getDataForController(hass, entry, controller):
    for controller in hass.data[DOMAIN][entry.entry_id]["allControllers"]:
        if controller == hass.data[DOMAIN][entry.entry_id]["controller"]:
            return hass.data[DOMAIN][entry.entry_id]
        elif (
            "childDevices" in hass.data[DOMAIN][entry.entry_id]
            and hass.data[DOMAIN][entry.entry_id]["childDevices"] is not False
        ):
            for childDevice in hass.data[DOMAIN][entry.entry_id]["childDevices"]:
                if controller == childDevice["controller"]:
                    return childDevice


def getNightModeMap():
    return {
        "inf_night_vision": "Infrared Mode",
        "wtl_night_vision": "Full Color Mode",
        "md_night_vision": "Smart Mode",
        "dbl_night_vision": "Doorbell Mode",
        "shed_night_vision": "Scheduled Mode",
    }


def getNightModeName(value: str):
    nightModeMap = getNightModeMap()
    if value in nightModeMap:
        return nightModeMap[value]
    return value


def getNightModeValue(value: str):
    night_mode_map = getNightModeMap()
    for key, val in night_mode_map.items():
        if val == value:
            return key
    return value


def convertBasicInfo(basicInfo):
    convertedBasicInfo = basicInfo
    convertedBasicInfo["device_alias"] = base64.b64decode(basicInfo["nickname"]).decode(
        "utf-8"
    )
    convertedBasicInfo["device_model"] = basicInfo["model"]
    convertedBasicInfo["sw_version"] = basicInfo["fw_ver"]
    convertedBasicInfo["hw_version"] = basicInfo["hw_ver"]
    return convertedBasicInfo


def getIP(data):
    # KLAP report IP in this function
    if (
        "basic_info" in data
        and data["basic_info"] is not None
        and "ip" in data["basic_info"]
    ):
        return data["basic_info"]["ip"]
    # cameras report IP in this function
    elif (
        "network_ip_info" in data
        and data["network_ip_info"] is not None
        and "network" in data["network_ip_info"]
        and "wan" in data["network_ip_info"]["network"]
        and "ipaddr" in data["network_ip_info"]["network"]["wan"]
    ):
        return data["network_ip_info"]["network"]["wan"]["ipaddr"]
    return False


def motionSensitivityFromData(motionDet):
    sensitivity_map = {"low": "low", "medium": "normal", "high": "high"}
    digital_map = {"20": "low", "50": "normal", "80": "high"}

    sensitivity = motionDet.get("sensitivity")
    if sensitivity in sensitivity_map:
        return sensitivity_map[sensitivity]

    return digital_map.get(motionDet.get("digital_sensitivity"))


def detectionSensitivityFromPercentage(value):
    sensitivity = tryParseInt(value)
    if sensitivity is None:
        return None
    if sensitivity <= 33:
        return "low"
    if sensitivity <= 66:
        return "normal"
    return "high"


def extractFieldByChannel(container, field):
    if not isinstance(container, dict):
        return None
    if field in container:
        return container[field]
    per_channel = {}
    for chn_key, chn_value in container.items():
        if isinstance(chn_value, dict) and field in chn_value:
            per_channel[str(chn_key)] = chn_value[field]
    if per_channel:
        return per_channel
    return None


def getLdcImageSection(ldc_data, section):
    if not ldc_data:
        return None
    entries = ldc_data if isinstance(ldc_data, list) else [ldc_data]
    result = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        image = entry.get("image")
        if not isinstance(image, dict):
            continue
        section_data = image.get(section)
        if section_data is None:
            continue
        if result is None:
            result = section_data
        elif isinstance(result, dict) and isinstance(section_data, dict):
            merged = dict(result)
            merged.update(section_data)
            result = merged
        else:
            result = section_data
    return result


def ldcHasField(rawData, section, field):
    ldc_data = rawData.get("getLdc", [])
    entries = ldc_data if isinstance(ldc_data, list) else [ldc_data]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        image = entry.get("image")
        if not isinstance(image, dict):
            continue
        section_data = image.get(section)
        if not isinstance(section_data, dict):
            continue
        if field in section_data:
            return True
        for value in section_data.values():
            if isinstance(value, dict) and field in value:
                return True
    return False


async def getCamData(hass, controller, chInfo=None):
    LOGGER.debug("getCamData")

    chn_id = []
    if chInfo:
        for lens in chInfo:
            chn_id.append(lens["chn_id"])
    data = await hass.async_add_executor_job(controller.getMost, [], chn_id)
    LOGGER.debug("Raw update data:")
    LOGGER.debug(data)

    camData = {}

    camData["raw"] = data

    camData["user"] = controller.user
    if controller.isKLAP:
        camData["basic_info"] = convertBasicInfo(data["get_device_info"][0])
    else:
        camData["basic_info"] = data["getDeviceInfo"][0]["device_info"]["basic_info"]

    try:
        motion_detection_data = data["getDetectionConfig"][0]["motion_detection"][
            "motion_det"
        ]
        motionDetectionData = (
            {"1": motion_detection_data} if chInfo is None else motion_detection_data
        )

        motion_detection_enabled = {
            str(key): motion_det["enabled"]
            for key, motion_det in motionDetectionData.items()
        }
        motion_detection_digital_sensitivity = {
            str(key): motion_det["digital_sensitivity"]
            for key, motion_det in motionDetectionData.items()
        }
        motion_detection_sensitivity = {
            str(key): motionSensitivityFromData(motion_det)
            for key, motion_det in motionDetectionData.items()
        }
    except Exception:
        motion_detection_enabled = None
        motion_detection_sensitivity = None
        motion_detection_digital_sensitivity = None
    camData["motion_detection_enabled"] = motion_detection_enabled
    camData["motion_detection_sensitivity"] = motion_detection_sensitivity
    camData["motion_detection_digital_sensitivity"] = (
        motion_detection_digital_sensitivity
    )

    try:
        dst_data = data["getDstRule"][0]["system"]["dst"]
    except Exception:
        dst_data = None
    camData["dst_data"] = dst_data

    try:
        clock_data = data["getClockStatus"][0]["system"]["clock_status"]
    except Exception:
        clock_data = None
    camData["clock_data"] = clock_data

    try:
        timezone_timezone = data["getTimezone"][0]["system"]["basic"]["timezone"]
    except Exception:
        timezone_timezone = None
    camData["timezone_timezone"] = timezone_timezone

    try:
        alert_event_types = data["getAlertEventType"][0]["msg_alarm"]["msg_alarm_type"]
    except Exception:
        alert_event_types = None
    camData["alert_event_types"] = alert_event_types

    try:
        timezone_zone_id = data["getTimezone"][0]["system"]["basic"]["zone_id"]
    except Exception:
        timezone_zone_id = None
    camData["timezone_zone_id"] = timezone_zone_id

    try:
        timezone_timing_mode = data["getTimezone"][0]["system"]["basic"]["timing_mode"]
    except Exception:
        timezone_timing_mode = None
    camData["timezone_timing_mode"] = timezone_timing_mode

    try:
        personDetectionData = data["getPersonDetectionConfig"][0]["people_detection"][
            "detection"
        ]
        if chInfo is None:
            personDetectionData = {"1": personDetectionData}
        person_detection_enabled = {}
        person_detection_sensitivity = {}
        for key, detectionData in personDetectionData.items():
            if not isinstance(detectionData, dict):
                continue
            key = str(key)
            person_detection_enabled[key] = detectionData.get("enabled")
            person_detection_sensitivity[key] = detectionSensitivityFromPercentage(
                detectionData.get("sensitivity")
            )
        if not person_detection_enabled:
            person_detection_enabled = None
        if not person_detection_sensitivity:
            person_detection_sensitivity = None
    except Exception:
        person_detection_enabled = None
        person_detection_sensitivity = None
    camData["person_detection_enabled"] = person_detection_enabled
    camData["person_detection_sensitivity"] = person_detection_sensitivity

    try:
        vehicleDetectionData = data["getVehicleDetectionConfig"][0][
            "vehicle_detection"
        ]["detection"]
        if chInfo is None:
            vehicleDetectionData = {"1": vehicleDetectionData}
        vehicle_detection_enabled = {}
        vehicle_detection_sensitivity = {}
        for key, detectionData in vehicleDetectionData.items():
            if not isinstance(detectionData, dict):
                continue
            key = str(key)
            vehicle_detection_enabled[key] = detectionData.get("enabled")
            vehicle_detection_sensitivity[key] = detectionSensitivityFromPercentage(
                detectionData.get("sensitivity")
            )
        if not vehicle_detection_enabled:
            vehicle_detection_enabled = None
        if not vehicle_detection_sensitivity:
            vehicle_detection_sensitivity = None
    except Exception:
        vehicle_detection_enabled = None
        vehicle_detection_sensitivity = None
    camData["vehicle_detection_enabled"] = vehicle_detection_enabled
    camData["vehicle_detection_sensitivity"] = vehicle_detection_sensitivity

    try:
        babyCryDetectionData = data["getBCDConfig"][0]["sound_detection"]["bcd"]
        babyCry_detection_enabled = babyCryDetectionData["enabled"]
        babyCry_detection_sensitivity = None

        sensitivity = babyCryDetectionData["sensitivity"]
        if sensitivity is not None:
            if sensitivity == "low":
                babyCry_detection_sensitivity = "low"
            elif sensitivity == "medium":
                babyCry_detection_sensitivity = "normal"
            else:
                babyCry_detection_sensitivity = "high"
    except Exception:
        babyCry_detection_enabled = None
        babyCry_detection_sensitivity = None
    camData["babyCry_detection_enabled"] = babyCry_detection_enabled
    camData["babyCry_detection_sensitivity"] = babyCry_detection_sensitivity

    try:
        petDetectionData = data["getPetDetectionConfig"][0]["pet_detection"][
            "detection"
        ]
        if chInfo is None:
            petDetectionData = {"1": petDetectionData}
        pet_detection_enabled = {}
        pet_detection_sensitivity = {}
        for key, detectionData in petDetectionData.items():
            if not isinstance(detectionData, dict):
                continue
            key = str(key)
            pet_detection_enabled[key] = detectionData.get("enabled")
            pet_detection_sensitivity[key] = detectionSensitivityFromPercentage(
                detectionData.get("sensitivity")
            )
        if not pet_detection_enabled:
            pet_detection_enabled = None
        if not pet_detection_sensitivity:
            pet_detection_sensitivity = None
    except Exception:
        pet_detection_enabled = None
        pet_detection_sensitivity = None
    camData["pet_detection_enabled"] = pet_detection_enabled
    camData["pet_detection_sensitivity"] = pet_detection_sensitivity

    try:
        barkDetectionData = data["getBarkDetectionConfig"][0]["bark_detection"][
            "detection"
        ]
        bark_detection_enabled = barkDetectionData["enabled"]
        bark_detection_sensitivity = None

        sensitivity = tryParseInt(barkDetectionData["sensitivity"])
        if sensitivity is not None:
            if sensitivity <= 33:
                bark_detection_sensitivity = "low"
            elif sensitivity <= 66:
                bark_detection_sensitivity = "normal"
            else:
                bark_detection_sensitivity = "high"
    except Exception:
        bark_detection_enabled = None
        bark_detection_sensitivity = None
    camData["bark_detection_enabled"] = bark_detection_enabled
    camData["bark_detection_sensitivity"] = bark_detection_sensitivity

    try:
        meowDetectionData = data["getMeowDetectionConfig"][0]["meow_detection"][
            "detection"
        ]
        meow_detection_enabled = meowDetectionData["enabled"]
        meow_detection_sensitivity = None

        sensitivity = tryParseInt(meowDetectionData["sensitivity"])
        if sensitivity is not None:
            if sensitivity <= 33:
                meow_detection_sensitivity = "low"
            elif sensitivity <= 66:
                meow_detection_sensitivity = "normal"
            else:
                meow_detection_sensitivity = "high"
    except Exception:
        meow_detection_enabled = None
        meow_detection_sensitivity = None
    camData["meow_detection_enabled"] = meow_detection_enabled
    camData["meow_detection_sensitivity"] = meow_detection_sensitivity

    try:
        glassDetectionData = data["getGlassDetectionConfig"][0]["glass_detection"][
            "detection"
        ]
        glass_detection_enabled = glassDetectionData["enabled"]
        glass_detection_sensitivity = None

        sensitivity = tryParseInt(glassDetectionData["sensitivity"])
        if sensitivity is not None:
            if sensitivity <= 33:
                glass_detection_sensitivity = "low"
            elif sensitivity <= 66:
                glass_detection_sensitivity = "normal"
            else:
                glass_detection_sensitivity = "high"
    except Exception:
        glass_detection_enabled = None
        glass_detection_sensitivity = None
    camData["glass_detection_enabled"] = glass_detection_enabled
    camData["glass_detection_sensitivity"] = glass_detection_sensitivity

    try:
        tamperDetectionData = data["getTamperDetectionConfig"][0]["tamper_detection"][
            "tamper_det"
        ]
        if chInfo is None:
            tamperDetectionData = {"1": tamperDetectionData}
        tamper_detection_enabled = {}
        tamper_detection_sensitivity = {}
        for key, detectionData in tamperDetectionData.items():
            if not isinstance(detectionData, dict):
                continue
            key = str(key)
            tamper_detection_enabled[key] = detectionData.get("enabled")
            sensitivity = detectionData.get("sensitivity")
            if sensitivity is None:
                tamper_detection_sensitivity[key] = None
            elif sensitivity == "medium":
                tamper_detection_sensitivity[key] = "normal"
            else:
                tamper_detection_sensitivity[key] = sensitivity
        if not tamper_detection_enabled:
            tamper_detection_enabled = None
        if not tamper_detection_sensitivity:
            tamper_detection_sensitivity = None
    except Exception:
        tamper_detection_enabled = None
        tamper_detection_sensitivity = None
    camData["tamper_detection_enabled"] = tamper_detection_enabled
    camData["tamper_detection_sensitivity"] = tamper_detection_sensitivity

    try:
        presets = {
            id: data["getPresetConfig"][0]["preset"]["preset"]["name"][key]
            for key, id in enumerate(
                data["getPresetConfig"][0]["preset"]["preset"]["id"]
            )
        }
    except Exception:
        presets = False

    try:
        privacy_mode = data["getLensMaskConfig"][0]["lens_mask"]["lens_mask_info"][
            "enabled"
        ]
    except Exception:
        privacy_mode = None
    camData["privacy_mode"] = privacy_mode

    try:
        notifications = data["getMsgPushConfig"][0]["msg_push"]["chn1_msg_push_info"][
            "notification_enabled"
        ]
    except Exception:
        notifications = None
    camData["notifications"] = notifications

    try:
        rich_notifications = data["getMsgPushConfig"][0]["msg_push"][
            "chn1_msg_push_info"
        ]["rich_notification_enabled"]
    except Exception:
        rich_notifications = None
    camData["rich_notifications"] = rich_notifications

    ldc_switch = getLdcImageSection(data.get("getLdc"), "switch")
    ldc_common = getLdcImageSection(data.get("getLdc"), "common")

    try:
        lens_distrotion_correction = extractFieldByChannel(ldc_switch, "ldc")
    except Exception:
        lens_distrotion_correction = None
    camData["lens_distrotion_correction"] = lens_distrotion_correction

    try:
        ldcStyle = extractFieldByChannel(ldc_common, "style")
    except Exception:
        ldcStyle = None
    camData["ldcStyle"] = ldcStyle

    try:
        light_frequency_mode = extractFieldByChannel(ldc_common, "light_freq_mode")
    except Exception:
        light_frequency_mode = None

    if light_frequency_mode is None:
        try:
            light_frequency_mode = extractFieldByChannel(
                data["getLightFrequencyInfo"][0]["image"]["common"], "light_freq_mode"
            )
        except Exception:
            light_frequency_mode = None
    camData["light_frequency_mode"] = light_frequency_mode

    try:
        night_vision_mode = extractFieldByChannel(
            data["getNightVisionModeConfig"][0]["image"]["switch"],
            "night_vision_mode",
        )
    except Exception:
        night_vision_mode = None
    camData["night_vision_mode"] = night_vision_mode

    try:
        diagnose_mode = data["getDiagnoseMode"][0]["system"]["sys"]
    except Exception:
        diagnose_mode = None
    camData["diagnose_mode"] = diagnose_mode

    try:
        cover_config = data["getCoverConfig"][0]["cover"]["cover"]
    except Exception:
        cover_config = None
    camData["cover_config"] = cover_config

    try:
        smart_track_config = data["getSmartTrackConfig"][0]["smart_track"][
            "smart_track_info"
        ]
    except Exception:
        smart_track_config = None
    camData["smart_track_config"] = smart_track_config

    try:
        network_ip_info = data["getDeviceIpAddress"][0]
    except Exception:
        network_ip_info = None
    camData["network_ip_info"] = network_ip_info

    try:
        night_vision_capability = data["getNightVisionCapability"][0][
            "image_capability"
        ]["supplement_lamp"]["night_vision_mode_range"]
    except Exception:
        night_vision_capability = None
    camData["night_vision_capability"] = night_vision_capability

    try:
        night_vision_mode_switching = extractFieldByChannel(ldc_common, "inf_type")
    except Exception:
        night_vision_mode_switching = None
    camData["night_vision_mode_switching"] = night_vision_mode_switching

    if night_vision_mode_switching is None:
        try:
            night_vision_mode_switching = extractFieldByChannel(
                data["getLightFrequencyInfo"][0]["image"]["common"], "inf_type"
            )
        except Exception:
            night_vision_mode_switching = None
        camData["night_vision_mode_switching"] = night_vision_mode_switching

    try:
        force_white_lamp_state = extractFieldByChannel(ldc_switch, "force_wtl_state")
    except Exception:
        force_white_lamp_state = None
    camData["force_white_lamp_state"] = force_white_lamp_state

    try:
        smartwtl_digital_level = extractFieldByChannel(
            ldc_common, "smartwtl_digital_level"
        )
    except Exception:
        smartwtl_digital_level = None
    camData["smartwtl_digital_level"] = smartwtl_digital_level

    try:
        flood_light_config = data["getFloodlightConfig"][0]["floodlight"]["config"]
    except Exception:
        flood_light_config = None
    camData["flood_light_config"] = flood_light_config

    try:
        flood_light_status = data["getFloodlightStatus"][0]["status"]
    except Exception:
        flood_light_status = None
    camData["flood_light_status"] = flood_light_status

    try:
        flood_light_capability = data["getFloodlightCapability"][0]["floodlight"][
            "capability"
        ]
    except Exception:
        flood_light_capability = None
    camData["flood_light_capability"] = flood_light_capability

    try:
        flip_type = extractFieldByChannel(ldc_switch, "flip_type")
        if isinstance(flip_type, dict):
            flip = {
                key: ("on" if value == "center" else "off")
                for key, value in flip_type.items()
            }
        elif flip_type is None:
            flip = None
        else:
            flip = "on" if flip_type == "center" else "off"
    except Exception:
        flip = None

    if flip is None:
        try:
            rotation_image = data["getRotationStatus"][0].get("image", {})
            rotation_switch = rotation_image.get("switch_chn") or rotation_image.get(
                "switch"
            )
            rotation_flip = extractFieldByChannel(rotation_switch, "flip_type")
            if isinstance(rotation_flip, dict):
                flip = {
                    key: ("on" if value == "center" else "off")
                    for key, value in rotation_flip.items()
                }
            elif rotation_flip is None:
                flip = None
            else:
                flip = "on" if rotation_flip == "center" else "off"
        except Exception:
            flip = None
    camData["flip"] = flip

    hubSiren = False
    alarmConfig = None
    alarmStatus = False
    alarmSirenTypeList = []
    if controller.isKLAP is False:
        try:
            if data["getSirenConfig"][0] != False:
                hubSiren = True
                sirenData = data["getSirenConfig"][0]
                alarmConfig = {
                    "typeOfAlarm": "getSirenConfig",
                    "siren_type": sirenData["siren_type"],
                    "siren_volume": sirenData["volume"],
                    "siren_duration": sirenData["duration"],
                }
        except Exception as err:
            LOGGER.error(f"getSirenConfig unexpected error {err=}, {type(err)=}")

    if controller.isKLAP is False:
        try:
            if not hubSiren and data["getAlarmConfig"][0] != False:
                alarmData = data["getAlarmConfig"][0]
                alarmConfig = {
                    "typeOfAlarm": "getAlarmConfig",
                    "mode": alarmData["alarm_mode"],
                    "automatic": alarmData["enabled"],
                }
                if "light_type" in alarmData:
                    alarmConfig["light_type"] = alarmData["light_type"]
                if "siren_type" in alarmData:
                    alarmConfig["siren_type"] = alarmData["siren_type"]
                if "siren_duration" in alarmData:
                    alarmConfig["siren_duration"] = alarmData["siren_duration"]
                if "alarm_duration" in alarmData:
                    alarmConfig["alarm_duration"] = alarmData["alarm_duration"]
                if "siren_volume" in alarmData:
                    alarmConfig["siren_volume"] = alarmData["siren_volume"]
                if "alarm_volume" in alarmData:
                    alarmConfig["alarm_volume"] = alarmData["alarm_volume"]

        except Exception as err:
            LOGGER.error(f"getAlarmConfig unexpected error {err=}, {type(err)=}")

    if controller.isKLAP is False:
        try:
            lastAlarmInfo = data["getLastAlarmInfo"][0]
            lastAlarmInfoMsgAlarm = (
                lastAlarmInfo.get("msg_alarm")
                if isinstance(lastAlarmInfo, dict)
                else None
            )
            alarmData = (
                lastAlarmInfoMsgAlarm.get("chn1_msg_alarm_info")
                if isinstance(lastAlarmInfoMsgAlarm, dict)
                else None
            )
            if alarmConfig is None and isinstance(alarmData, dict):
                alarmConfig = {
                    "typeOfAlarm": "getAlarm",
                    "mode": alarmData["alarm_mode"],
                    "automatic": alarmData["enabled"],
                }
                if "light_type" in alarmData:
                    alarmConfig["light_type"] = alarmData["light_type"]
                if "siren_type" in alarmData:
                    alarmConfig["siren_type"] = alarmData["siren_type"]
                if "alarm_type" in alarmData:
                    alarmConfig["siren_type"] = alarmData["alarm_type"]
                if "siren_duration" in alarmData:
                    alarmConfig["siren_duration"] = alarmData["siren_duration"]
                if "alarm_duration" in alarmData:
                    alarmConfig["alarm_duration"] = alarmData["alarm_duration"]
                if "siren_volume" in alarmData:
                    alarmConfig["siren_volume"] = alarmData["siren_volume"]
                if "alarm_volume" in alarmData:
                    alarmConfig["alarm_volume"] = alarmData["alarm_volume"]
        except Exception as err:
            LOGGER.error(f"getLastAlarmInfo unexpected error {err=}, {type(err)=}")

    if controller.isKLAP is False:
        try:
            for alertConfig in data["getAlertConfig"]:
                alertConfigMsgAlarm = (
                    alertConfig.get("msg_alarm")
                    if isinstance(alertConfig, dict)
                    else None
                )
                alarmData = (
                    alertConfigMsgAlarm.get("chn1_msg_alarm_info")
                    if isinstance(alertConfigMsgAlarm, dict)
                    else None
                )
                if alarmConfig is None and isinstance(alarmData, dict):
                    alarmConfig = {
                        "typeOfAlarm": "getAlertConfig",
                        "mode": alarmData["alarm_mode"],
                        "automatic": alarmData["enabled"],
                        "alert_config": alarmData,
                    }
                    if "light_type" in alarmData:
                        alarmConfig["light_type"] = alarmData["light_type"]
                    if "siren_type" in alarmData:
                        alarmConfig["siren_type"] = alarmData["siren_type"]
                    if "alarm_type" in alarmData:
                        alarmConfig["siren_type"] = alarmData["alarm_type"]
                    if "siren_duration" in alarmData:
                        alarmConfig["siren_duration"] = alarmData["siren_duration"]
                    if "alarm_duration" in alarmData:
                        alarmConfig["alarm_duration"] = alarmData["alarm_duration"]
                    if "siren_volume" in alarmData:
                        alarmConfig["siren_volume"] = alarmData["siren_volume"]
                    if "alarm_volume" in alarmData:
                        alarmConfig["alarm_volume"] = alarmData["alarm_volume"]
                    break
        except Exception as err:
            LOGGER.error(f"getAlertConfig unexpected error {err=}, {type(err)=}")

    if controller.isKLAP is False:
        try:
            if (
                data["getSirenStatus"][0] is not False
                and "status" in data["getSirenStatus"][0]
            ):
                alarmStatus = data["getSirenStatus"][0]["status"]
        except Exception as err:
            LOGGER.error(f"getSirenStatus unexpected error {err=}, {type(err)=}")

    if controller.isKLAP is False:
        if alarmConfig is not None:
            try:
                if (
                    data["getSirenTypeList"][0] is not False
                    and "siren_type_list" in data["getSirenTypeList"][0]
                ):
                    alarmSirenTypeList = data["getSirenTypeList"][0]["siren_type_list"]
            except Exception as err:
                LOGGER.error(f"getSirenTypeList unexpected error {err=}, {type(err)=}")

    if controller.isKLAP is False:
        if len(alarmSirenTypeList) == 0:
            try:
                if (
                    data["getAlertTypeList"][0] is not False
                    and "msg_alarm" in data["getAlertTypeList"][0]
                    and "alert_type" in data["getAlertTypeList"][0]["msg_alarm"]
                    and "alert_type_list"
                    in data["getAlertTypeList"][0]["msg_alarm"]["alert_type"]
                ):
                    alarmSirenTypeList = data["getAlertTypeList"][0]["msg_alarm"][
                        "alert_type"
                    ]["alert_type_list"]
            except Exception as err:
                LOGGER.error(f"getSirenTypeList unexpected error {err=}, {type(err)=}")

    if len(alarmSirenTypeList) == 0:
        # Some cameras have hardcoded 0 and 1 values (Siren, Tone)
        alarmSirenTypeList.append("Siren")
        alarmSirenTypeList.append("Tone")

    alarm_user_sounds = None
    try:
        for alertConfig in data["getAlertConfig"]:
            if (
                alertConfig is not False
                and "msg_alarm" in alertConfig
                and "usr_def_audio" in alertConfig["msg_alarm"]
                and (alarm_user_sounds is None or len(alarm_user_sounds) == 0)
            ):
                alarm_user_sounds = []
                for alarm_sound in alertConfig["msg_alarm"]["usr_def_audio"]:
                    first_key = next(iter(alarm_sound))
                    first_value = alarm_sound[first_key]
                    alarm_user_sounds.append(first_value)
    except Exception:
        alarm_user_sounds = None

    alarm_user_start_id = None
    try:
        for alertConfig in data["getAlertConfig"]:
            if (
                alertConfig is not False
                and "msg_alarm" in alertConfig
                and "capability" in alertConfig["msg_alarm"]
                and "usr_def_start_file_id" in alertConfig["msg_alarm"]["capability"]
                and alarm_user_start_id is None
            ):
                alarm_user_start_id = alertConfig["msg_alarm"]["capability"][
                    "usr_def_start_file_id"
                ]
    except Exception:
        alarm_user_start_id = None
    camData["alarm_user_start_id"] = alarm_user_start_id
    camData["alarm_user_sounds"] = alarm_user_sounds
    camData["alarm_config"] = alarmConfig
    camData["alarm_status"] = alarmStatus
    camData["alarm_is_hubSiren"] = hubSiren
    camData["alarm_siren_type_list"] = alarmSirenTypeList

    try:
        if (
            "image_capability" in data["getNightVisionCapability"][0]
            and "supplement_lamp"
            in data["getNightVisionCapability"][0]["image_capability"]
        ):
            nightVisionCapability = data["getNightVisionCapability"][0][
                "image_capability"
            ]["supplement_lamp"]
    except Exception:
        nightVisionCapability = None
    camData["nightVisionCapability"] = nightVisionCapability

    try:
        led = data["getLedStatus"][0]["led"]["config"]["enabled"]
    except Exception:
        led = None

    if led is None:
        led = "on" if data["get_device_info"][0]["led_off"] == 0 else "off"
    camData["led"] = led

    # todo rest
    try:
        auto_track = data["getTargetTrackConfig"][0]["target_track"][
            "target_track_info"
        ]["enabled"]
    except Exception:
        auto_track = None
    camData["auto_track"] = auto_track

    if presets:
        camData["presets"] = presets
    else:
        camData["presets"] = {}

    try:
        patrolStatus = data["getPatrolAction"][0]["patrol"]["patrol"]["action"]
    except Exception:
        patrolStatus = None
    camData["patrol_status"] = patrolStatus

    try:
        firmwareUpdateStatus = data["getFirmwareUpdateStatus"][0]["cloud_config"]
    except Exception:
        firmwareUpdateStatus = None
    camData["firmwareUpdateStatus"] = firmwareUpdateStatus

    try:
        childDevices = data["getChildDeviceList"][0]
    except Exception:
        childDevices = None
    camData["childDevices"] = childDevices

    try:
        whitelampConfigForceTime = extractFieldByChannel(
            data["getWhitelampConfig"][0]["image"]["switch"], "wtl_force_time"
        )
    except Exception:
        whitelampConfigForceTime = None
    camData["whitelampConfigForceTime"] = whitelampConfigForceTime

    try:
        whitelampConfigIntensity = extractFieldByChannel(
            data["getWhitelampConfig"][0]["image"]["switch"], "wtl_intensity_level"
        )
    except Exception:
        whitelampConfigIntensity = None
    camData["whitelampConfigIntensity"] = whitelampConfigIntensity

    try:
        whitelampStatus = data["getWhitelampStatus"][0]["status"]
    except Exception:
        whitelampStatus = None
    camData["whitelampStatus"] = whitelampStatus

    try:
        sdCardData = []
        for hdd in data["getSdCardStatus"][0]["harddisk_manage"]["hd_info"]:
            sdCardData.append(hdd["hd_info_1"])
    except Exception:
        sdCardData = []
    camData["sdCardData"] = sdCardData

    try:
        recordPlan = data["getRecordPlan"][0]["record_plan"]["chn1_channel"]
    except Exception:
        recordPlan = None
    camData["recordPlan"] = recordPlan

    try:
        microphoneVolume = data["getAudioConfig"][0]["audio_config"]["microphone"][
            "volume"
        ]
    except Exception:
        microphoneVolume = None
    camData["microphoneVolume"] = microphoneVolume

    try:
        microphoneMute = data["getAudioConfig"][0]["audio_config"]["microphone"]["mute"]
    except Exception:
        microphoneMute = None
    camData["microphoneMute"] = microphoneMute

    try:
        microphoneNoiseCancelling = data["getAudioConfig"][0]["audio_config"][
            "microphone"
        ]["noise_cancelling"]
    except Exception:
        microphoneNoiseCancelling = None
    camData["microphoneNoiseCancelling"] = microphoneNoiseCancelling

    try:
        speakerVolume = data["getAudioConfig"][0]["audio_config"]["speaker"]["volume"]
    except Exception:
        speakerVolume = None
    camData["speakerVolume"] = speakerVolume

    try:
        record_audio = (
            data["getAudioConfig"][0]["audio_config"]["record_audio"]["enabled"] == "on"
        )
    except Exception:
        record_audio = None
    camData["record_audio"] = record_audio

    try:
        autoUpgradeEnabled = data["getFirmwareAutoUpgradeConfig"][0]["auto_upgrade"][
            "common"
        ]["enabled"]
    except Exception:
        autoUpgradeEnabled = None
    camData["autoUpgradeEnabled"] = autoUpgradeEnabled

    try:
        rebootConfig = data["getReboot"][0]["timing_reboot"]["reboot"]
    except Exception:
        rebootConfig = None
    camData["rebootConfig"] = rebootConfig
    if isinstance(rebootConfig, dict):
        camData["rebootEnabled"] = rebootConfig.get("enabled")
        camData["rebootTime"] = rebootConfig.get("time")
        camData["rebootDay"] = rebootConfig.get("day")
        camData["rebootRandomRange"] = rebootConfig.get("random_range")
        camData["rebootLastTime"] = rebootConfig.get("last_reboot_time")
    else:
        camData["rebootEnabled"] = None
        camData["rebootTime"] = None
        camData["rebootDay"] = None
        camData["rebootRandomRange"] = None
        camData["rebootLastTime"] = None

    try:
        connectionInformation = data["getConnectionType"][0]
    except Exception:
        connectionInformation = None

    if connectionInformation is None:
        connectionInformation = {}
        try:
            connectionInformation["ssid"] = base64.b64decode(
                data["get_device_info"][0]["ssid"]
            ).decode("utf-8")
        except Exception:
            pass
        try:
            connectionInformation["rssiValue"] = data["get_device_info"][0]["rssi"]

        except Exception:
            pass
    camData["connectionInformation"] = connectionInformation

    try:
        videoCapability = data["getVideoCapability"][0]
    except Exception:
        videoCapability = None
    camData["videoCapability"] = videoCapability

    try:
        allChnInfo = data["getAllChnInfo"][0]
    except Exception:
        allChnInfo = None
    camData["allChnInfo"] = allChnInfo

    try:
        dualCamCapability = data["getDualCamCapability"][0]
    except Exception:
        dualCamCapability = None
    camData["dualCamCapability"] = dualCamCapability

    try:
        videoQualities = data["getVideoQualities"][0]
    except Exception:
        videoQualities = None
    camData["videoQualities"] = videoQualities

    camData["updated"] = datetime.datetime.utcnow().timestamp()

    try:
        chimeAlarmConfigurations = {}
        count = 0
        for chimeAlarmConfiguration in data["get_chime_alarm_configure"]:
            chimeAlarmConfigurations[data["get_pair_list"][0]["mac_list"][count]] = (
                chimeAlarmConfiguration
            )
            count += 1
    except Exception:
        chimeAlarmConfigurations = None
    camData["chimeAlarmConfigurations"] = chimeAlarmConfigurations

    try:
        supportAlarmTypeList = data["get_support_alarm_type_list"][0]
    except Exception:
        supportAlarmTypeList = None
    camData["supportAlarmTypeList"] = supportAlarmTypeList

    try:
        if isinstance(data["getQuickRespList"], list):
            camData["quick_response"] = data["getQuickRespList"][0]["quick_response"][
                "quick_resp_audio"
            ]
        elif isinstance(data["getQuickRespList"], dict):
            camData["quick_response"] = data["getQuickRespList"]["quick_resp_audio"]
        else:
            LOGGER.warning("Quick response data is not in expected format")
    except Exception:
        camData["quick_response"] = None

    try:
        dualLinkageTargetSetting = data["readLinkageTargetSetting"][0][
            "dual_cam_linkage"
        ]
    except Exception:
        dualLinkageTargetSetting = None
    camData["dualLinkageTargetSetting"] = dualLinkageTargetSetting

    try:
        dualLinkageCapability = data["getLinkageTargetCapability"][0][
            "dual_cam_linkage"
        ]["linkage_target_capability"]
    except Exception:
        dualLinkageCapability = None
    camData["dualLinkageCapability"] = dualLinkageCapability

    try:
        dualCamLinkageEnabled = data["getDualCamLinkage"][0]["dual_cam_linkage"][
            "linkage_state"
        ]["enabled"]
        dualCamLinkageType = data["getDualCamLinkage"][0]["dual_cam_linkage"][
            "linkage_state"
        ]["linkage_type"]
    except Exception:
        dualCamLinkageEnabled = None
        dualCamLinkageType = None
    camData["dualCamLinkageEnabled"] = dualCamLinkageEnabled
    camData["dualCamLinkageType"] = dualCamLinkageType

    LOGGER.debug("getCamData - done")
    LOGGER.debug("Processed update data:")
    LOGGER.debug(camData)
    return camData


def convert_to_timestamp(date_string):
    date_format = "%Y%m%d"
    try:
        date = datetime.datetime.strptime(date_string, date_format)
        timestamp = datetime.datetime.timestamp(date)
        return int(timestamp)
    except ValueError:
        raise Exception(
            "Invalid date format. Please provide a date in the format 'YYYYMMDD'."
        )


async def update_listener(hass, entry):
    """Handle options update."""
    await async_reorganize_media_storage(hass, entry.entry_id)
    host = entry.data.get(CONF_IP_ADDRESS)
    controlPort = entry.data.get(CONTROL_PORT)
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    motionSensor = entry.data.get(ENABLE_MOTION_SENSOR)
    enableTimeSync = entry.data.get(ENABLE_TIME_SYNC)
    cloud_password = entry.data.get(CLOUD_PASSWORD)
    try:
        newUUID = hashlib.md5(
            (str(host) + str(username) + str(password) + str(cloud_password)).encode()
        ).hexdigest()
        # only update controller if auth data changed
        if newUUID != hass.data[DOMAIN][entry.entry_id]["uuid"]:
            hass.data[DOMAIN][entry.entry_id]["uuid"] = newUUID
            if (
                hass.data[DOMAIN][entry.entry_id]["controller"]
                in hass.data[DOMAIN][entry.entry_id]["allControllers"]
            ):
                hass.data[DOMAIN][entry.entry_id]["allControllers"].remove(
                    hass.data[DOMAIN][entry.entry_id]["controller"]
                )
            if cloud_password != "":
                tapoController = await hass.async_add_executor_job(
                    registerController,
                    host,
                    controlPort,
                    "admin",
                    cloud_password,
                    "",
                    "",
                    None,
                    None,
                    hass,
                )
            else:
                tapoController = await hass.async_add_executor_job(
                    registerController,
                    host,
                    controlPort,
                    username,
                    password,
                    "",
                    "",
                    None,
                    None,
                    hass,
                )
            hass.data[DOMAIN][entry.entry_id]["usingCloudPassword"] = (
                cloud_password != ""
            )
            hass.data[DOMAIN][entry.entry_id]["controller"] = tapoController
            hass.data[DOMAIN][entry.entry_id]["allControllers"].append(tapoController)
    except Exception:
        LOGGER.error(
            "Authentication to Tapo camera failed."
            + " Please restart the camera and try again."
        )

    for entity in hass.data[DOMAIN][entry.entry_id]["entities"]:
        if "_host" in entity:
            entity._host = host
        if "_username" in entity:
            entity._username = username
        if "_password" in entity:
            entity._password = password
    if hass.data[DOMAIN][entry.entry_id]["events"]:
        await hass.data[DOMAIN][entry.entry_id]["events"].async_stop()
    if hass.data[DOMAIN][entry.entry_id]["motionSensorCreated"]:
        await hass.config_entries.async_forward_entry_unload(entry, "binary_sensor")
        hass.data[DOMAIN][entry.entry_id]["motionSensorCreated"] = False
    if motionSensor or enableTimeSync:
        onvifDevice = await initOnvifEvents(hass, host, username, password)
        hass.data[DOMAIN][entry.entry_id]["eventsDevice"] = onvifDevice["device"]
        hass.data[DOMAIN][entry.entry_id]["onvifManagement"] = onvifDevice[
            "device_mgmt"
        ]
        if motionSensor:
            await setupOnvif(hass, entry)

    if entry.entry_id in hass.data.get(DOMAIN, {}):
        hass.data[DOMAIN][entry.entry_id]["mediaSyncColdDir"] = False


async def getLatestFirmwareVersion(hass, config_entry, entry, controller):
    entry["lastFirmwareCheck"] = datetime.datetime.utcnow().timestamp()
    try:
        updateInfo = await hass.async_add_executor_job(controller.isUpdateAvailable)
        if (
            "version"
            in updateInfo["result"]["responses"][1]["result"]["cloud_config"][
                "upgrade_info"
            ]
        ):
            updateInfo = updateInfo["result"]["responses"][1]["result"]["cloud_config"][
                "upgrade_info"
            ]
        else:
            updateInfo = False
    except Exception:
        updateInfo = False
    return updateInfo


async def syncTime(hass, entry_id):
    device_mgmt = hass.data[DOMAIN][entry_id]["onvifManagement"]
    if device_mgmt:
        LOGGER.debug(
            "Syncing time for "
            + hass.data[DOMAIN][entry_id]["name"]
            + ", timezone offset is "
            + str(hass.data[DOMAIN][entry_id]["timezoneOffset"])
            + "..."
        )
        isDST = dt_util.now().dst() != datetime.timedelta(0)

        timeSyncDST = int(hass.data[DOMAIN][entry_id][TIME_SYNC_DST])
        timeSyncNDST = int(hass.data[DOMAIN][entry_id][TIME_SYNC_NDST])

        LOGGER.debug("Is DST: " + str(isDST))
        LOGGER.debug("DST offset: " + str(timeSyncDST))
        LOGGER.debug("Non DST offset: " + str(timeSyncNDST))
        now = dt_util.utcnow()

        LOGGER.debug("UTC Home Assistant time: " + str(now))
        LOGGER.debug("Local Home Assistant time: " + str(dt_util.as_local(now)))

        adjustment_hours = timeSyncDST if isDST else timeSyncNDST
        adjusted_time = now + datetime.timedelta(hours=adjustment_hours)

        time_params = device_mgmt.create_type("SetSystemDateAndTime")
        time_params.DateTimeType = "Manual"
        time_params.DaylightSavings = isDST
        time_params.UTCDateTime = {
            "Date": {
                "Year": adjusted_time.year,
                "Month": adjusted_time.month,
                "Day": adjusted_time.day,
            },
            "Time": {
                "Hour": adjusted_time.hour,
                "Minute": adjusted_time.minute,
                "Second": adjusted_time.second,
            },
        }
        LOGGER.debug(
            "Sending time parameters to " + hass.data[DOMAIN][entry_id]["name"] + ":"
        )
        LOGGER.debug(time_params)
        await device_mgmt.SetSystemDateAndTime(time_params)
        LOGGER.debug(
            "Finished synchronizing time successfully. Setting last time sync to: "
            + str(now)
        )
        hass.data[DOMAIN][entry_id]["lastTimeSync"] = now.timestamp()
    else:
        LOGGER.warning(
            "Onvif has not been initialized yet, unable to synchronize time."
        )


async def setupOnvif(hass, entry):
    LOGGER.debug("setupOnvif - entry")
    if hass.data[DOMAIN][entry.entry_id]["eventsDevice"]:
        LOGGER.debug("Setting up onvif...")
        hass.data[DOMAIN][entry.entry_id]["events"] = EventManager(
            hass,
            hass.data[DOMAIN][entry.entry_id]["eventsDevice"],
            entry,
            hass.data[DOMAIN][entry.entry_id]["name"],
        )

        hass.data[DOMAIN][entry.entry_id]["eventsSetup"] = await setupEvents(
            hass, entry
        )


async def setupEvents(hass, config_entry):
    LOGGER.debug("setupEvents - entry")
    shouldUseWebhooks = (
        isUsingHTTPS(hass) is False and config_entry.data.get(ENABLE_WEBHOOKS) is True
    )
    LOGGER.debug("Using HTTPS: " + str(isUsingHTTPS(hass)))
    LOGGER.debug(
        "Webhook enabled: " + str(config_entry.data.get(ENABLE_WEBHOOKS) is True)
    )
    LOGGER.debug("Using Webhooks: " + str(shouldUseWebhooks))
    if (
        hass.data[DOMAIN][config_entry.entry_id]["events"] is not False
        and not hass.data[DOMAIN][config_entry.entry_id]["events"].started
    ):
        LOGGER.debug("Setting up events...")
        events = hass.data[DOMAIN][config_entry.entry_id]["events"]
        onvif_capabilities = await hass.data[DOMAIN][config_entry.entry_id][
            "eventsDevice"
        ].get_capabilities()
        onvif_capabilities = onvif_capabilities or {}
        pull_point_support = onvif_capabilities.get("Events", {}).get(
            "WSPullPointSupport"
        )
        LOGGER.debug("WSPullPointSupport: %s", pull_point_support)
        if await events.async_start(pull_point_support is not False, shouldUseWebhooks):
            LOGGER.debug("Events started.")
            if not hass.data[DOMAIN][config_entry.entry_id]["motionSensorCreated"]:
                hass.data[DOMAIN][config_entry.entry_id]["motionSensorCreated"] = True
                if hass.data[DOMAIN][config_entry.entry_id].get("eventsListener"):
                    hass.data[DOMAIN][config_entry.entry_id][
                        "eventsListener"
                    ].createBinarySensor()
                else:
                    LOGGER.error(
                        "Trying to create motion sensor but motion listener not set up!"
                    )

                if hass.data[DOMAIN][config_entry.entry_id].get("eventsEntityListener"):
                    hass.data[DOMAIN][config_entry.entry_id][
                        "eventsEntityListener"
                    ].createEventEntities()

                LOGGER.debug(
                    "Binary sensor and event creation for motion has been forwarded to component."
                )
            return True
        else:
            return False


def build_device_info(attributes: dict) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, slugify(f"{attributes['mac']}_tapo_control"))},
        connections={("mac", attributes["mac"])},
        name=attributes["device_alias"],
        manufacturer=BRAND,
        model=attributes["device_model"],
        sw_version=attributes["sw_version"],
        hw_version=attributes["hw_version"],
    )


def pytapoFunctionMap(pytapoFunctionName):
    if pytapoFunctionName == "getPrivacyMode":
        return ["getLensMaskConfig"]
    elif pytapoFunctionName == "getNotificationsEnabled":
        return ["getMsgPushConfig"]
    elif pytapoFunctionName == "getBasicInfo":
        return ["getDeviceInfo"]
    elif pytapoFunctionName == "getMotionDetection":
        return ["getDetectionConfig"]
    elif pytapoFunctionName == "getPersonDetection":
        return ["getPersonDetectionConfig"]
    elif pytapoFunctionName == "getVehicleDetection":
        return ["getVehicleDetectionConfig"]
    elif pytapoFunctionName == "getBabyCryDetection":
        return ["getBCDConfig"]
    elif pytapoFunctionName == "getPetDetection":
        return ["getPetDetectionConfig"]
    elif pytapoFunctionName == "getBarkDetection":
        return ["getBarkDetectionConfig"]
    elif pytapoFunctionName == "getMeowDetection":
        return ["getMeowDetectionConfig"]
    elif pytapoFunctionName == "getGlassBreakDetection":
        return ["getGlassDetectionConfig"]
    elif pytapoFunctionName == "getTamperDetection":
        return ["getTamperDetectionConfig"]
    elif pytapoFunctionName == "getLdc":
        return ["getLensDistortionCorrection"]
    elif pytapoFunctionName == "getAlarm":
        return ["getLastAlarmInfo", "getAlarmConfig"]
    elif pytapoFunctionName == "getLED":
        return ["getLedStatus"]
    elif pytapoFunctionName == "getAutoTrackTarget":
        return ["getTargetTrackConfig"]
    elif pytapoFunctionName == "getPresets":
        return ["getPresetConfig"]
    elif pytapoFunctionName == "getCruise":
        return ["getPatrolAction"]
    elif pytapoFunctionName == "getLightFrequencyMode":
        return ["getLightFrequencyInfo", "getLightFrequencyCapability"]
    elif pytapoFunctionName == "getChildDevices":
        return ["getChildDeviceList"]
    elif pytapoFunctionName == "getForceWhitelampState":
        return ["getLdc"]
    elif pytapoFunctionName == "getDayNightMode":
        return ["getLightFrequencyInfo", "getNightVisionModeConfig"]
    elif pytapoFunctionName == "getImageFlipVertical":
        return ["getRotationStatus", "getLdc"]
    elif pytapoFunctionName == "getLensDistortionCorrection":
        return ["getLdc"]
    return [pytapoFunctionName]


def isCacheSupported(check_function, rawData):
    rawFunctions = pytapoFunctionMap(check_function)
    for function in rawFunctions:
        if function in rawData:
            if rawData[function][0]:
                if check_function == "getForceWhitelampState":
                    return ldcHasField(rawData, "switch", "force_wtl_state")
                elif check_function == "getDayNightMode":
                    if (
                        "image" in rawData["getLightFrequencyInfo"][0]
                        and "common" in rawData["getLightFrequencyInfo"][0]["image"]
                    ):
                        common = rawData["getLightFrequencyInfo"][0]["image"]["common"]
                        if isinstance(common, dict) and "inf_type" in common:
                            return True
                        if isinstance(common, dict):
                            for entry in common.values():
                                if isinstance(entry, dict) and "inf_type" in entry:
                                    return True
                    return False
                elif check_function == "getImageFlipVertical":
                    if ldcHasField(rawData, "switch", "flip_type"):
                        return True
                    try:
                        rotation_image = rawData["getRotationStatus"][0].get(
                            "image", {}
                        )
                        rotation_switch = rotation_image.get(
                            "switch_chn"
                        ) or rotation_image.get("switch")
                        if isinstance(rotation_switch, dict):
                            if "flip_type" in rotation_switch:
                                return True
                            for entry in rotation_switch.values():
                                if isinstance(entry, dict) and "flip_type" in entry:
                                    return True
                    except Exception:
                        pass
                    return False
                elif check_function == "getLensDistortionCorrection":
                    return ldcHasField(rawData, "switch", "ldc")
                return True
            else:
                raise Exception(
                    f"Capability {check_function} (mapped to:{function}) cached but not supported."
                )
    return False


async def scheduleAll(hass, device, entry, mediaSync):
    LOGGER.debug("scheduleAll for " + device["name"] + " called.")
    sync_source = entry.data.get(
        RECORDINGS_SOURCE,
        entry.data.get("media_sync_source", RECORDINGS_SOURCE_SD),
    )
    if sync_source == RECORDINGS_SOURCE_TAPO_CARE:
        device["initialMediaScanDone"] = True
        device["mediaSyncAvailable"] = True
        if device["mediaSyncScheduled"] is False:
            device["mediaSyncScheduled"] = True
            LOGGER.debug("Scheduling media sync for Tapo Care")
            callback = partial(mediaSync, entry=entry, device=device)
            entry.async_on_unload(
                async_track_time_interval(
                    hass,
                    callback,
                    datetime.timedelta(seconds=60),
                )
            )
        return

    if device["mediaSyncAvailable"]:
        if (
            device["initialMediaScanDone"] is True
            and device["mediaSyncScheduled"] is False
        ):
            device["mediaSyncScheduled"] = True
            LOGGER.debug("Scheduling media sync")
            callback = partial(mediaSync, entry=entry, device=device)

            entry.async_on_unload(
                async_track_time_interval(
                    hass,
                    callback,
                    datetime.timedelta(seconds=60),
                )
            )
        elif device["initialMediaScanRunning"] is False:
            LOGGER.debug("Media scan running")
            device["initialMediaScanRunning"] = True
            try:
                await hass.async_add_executor_job(
                    device["controller"].getRecordingsList
                )
                hass.async_create_background_task(
                    findMedia(hass, device, entry),
                    "findMedia",
                )
            except Exception as err:
                device["initialMediaScanDone"] = True
                device["mediaSyncAvailable"] = False
                enableMediaSync = device[ENABLE_MEDIA_SYNC]
                errMsg = "Disabling media sync as there was error returned from getRecordingsList. Do you have SD card inserted?"
                if enableMediaSync:
                    LOGGER.warning(errMsg)
                    LOGGER.warning(device["name"] + ": " + str(err))
                else:
                    LOGGER.info(errMsg)
                    LOGGER.info(device["name"] + ": " + str(err))


async def check_functionality(entry, hass, cls, check_function):
    try:
        if check_function == "getAlarm":
            alarm_config = entry.get("camData", {}).get("alarm_config")
            if (
                isinstance(alarm_config, dict)
                and alarm_config.get("typeOfAlarm") in ALARM_CONFIG_TYPES
            ):
                LOGGER.debug(
                    f"Found parsed alarm config, creating {cls.__name__}"
                )
                return True

        if isCacheSupported(check_function, entry["camData"]["raw"]):
            LOGGER.debug(
                f"Found cached capability {check_function}, creating {cls.__name__}"
            )
            return True
        else:
            if (
                entry["controller"].isKLAP is False
            ):  # no uncached entries for klap devices, so no need to check them
                LOGGER.debug(
                    f"Capability {check_function} not found, querying again..."
                )
                result = await hass.async_add_executor_job(
                    getattr(entry["controller"], check_function)
                )
                LOGGER.debug(result)
                LOGGER.debug(f"Creating {cls.__name__}")
                return True
    except Exception as err:
        LOGGER.info(f"Camera does not support {cls.__name__}: {err}")
        return False
    return False


async def check_and_create(entry, hass, cls, check_function, config_entry):
    if await check_functionality(entry, hass, cls, check_function):
        try:
            return cls(entry, hass, config_entry)
        except Exception as err:
            LOGGER.info(f"Camera does not support {cls.__name__}: {err}")
            return None
    return None
