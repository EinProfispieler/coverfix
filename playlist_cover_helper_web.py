#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import hashlib
import platform
import shutil
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple


HOST = "127.0.0.1"
PORT = 8765
APP_TITLE = "CoverFix"
APP_GITHUB_URL = "https://github.com/EinProfispieler/coverfix"
APP_BRAND_IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "Epanda.png"
OSASCRIPT_TIMEOUT_SEC = 45
OUT_DIR = Path.home() / ".coverfix" / "covers"
LEGACY_OUT_DIR = Path.home() / ".playlist-cover-helper" / "covers"
OUT_DIR.mkdir(parents=True, exist_ok=True)
ARTWORK_DB = Path.home() / "Library/Containers/com.apple.AMPArtworkAgent/Data/Documents/artworkd.sqlite"
ARTWORK_DIR = Path.home() / "Library/Containers/com.apple.AMPArtworkAgent/Data/Documents/artwork"
FMT_JPEG = 0x4A504547  # JPEG
FMT_PNG = 0x504E4766  # PNGf
FMT_TIFF = 0x54494646  # TIFF
KIND_CUSTOM = 102
KIND_LABELS = {
    12: "Catalog",
    45: "Catalog",
    60: "Remote",
    63: "Remote",
    KIND_CUSTOM: "Custom",
}
IS_WINDOWS = platform.system() == "Windows"
IS_DARWIN = platform.system() == "Darwin"


@dataclass
class Playlist:
    pid: str
    name: str
    track_count: int
    first_has_art: bool


def runtime_info() -> Dict[str, object]:
    system_name = platform.system()
    supported = IS_DARWIN
    can_control_music = IS_DARWIN
    if IS_WINDOWS:
        reason = "Windows is not supported. Cover operations require Apple Music and macOS system tools."
    elif not IS_DARWIN:
        reason = "This app currently supports macOS only."
    else:
        reason = ""
    return {
        "system": system_name,
        "supported": supported,
        "can_control_music": can_control_music,
        "reason": reason,
        "script_path": str(Path(__file__).resolve()),
    }


def require_supported_runtime() -> Tuple[bool, str]:
    info = runtime_info()
    if bool(info["supported"]):
        return True, "OK"
    return False, str(info["reason"])


def _run_osascript(script: str) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=OSASCRIPT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"AppleScript timed out after {OSASCRIPT_TIMEOUT_SEC}s. "
            "Music may be busy or waiting for permission.",
        )
    if proc.returncode != 0:
        err = _normalize_osascript_error(proc.stderr, proc.stdout)
        return False, err
    return True, proc.stdout.strip()


def _normalize_osascript_error(stderr_text: str, stdout_text: str) -> str:
    raw = (stderr_text or "").strip() or (stdout_text or "").strip() or "unknown AppleScript error"
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    filtered = [
        ln
        for ln in lines
        if "TISFileInterrogator" not in ln
        and "Keyboard Layouts:" not in ln
        and "Error received in message reply handler" not in ln
    ]
    joined = "\n".join(filtered if filtered else lines)
    if "Connection Invalid error for service com.apple.hiservices-xpcservice." in raw:
        return (
            "AppleScript service connection failed (hiservices-xpcservice). "
            "Try rebooting macOS, then re-open Music and CoverFix. "
            "If this persists, run `python3 playlist_cover_helper_web.py doctor`."
        )
    if "(-1743)" in raw:
        return (
            "Automation permission denied. Allow Terminal/Python to control Music in "
            "System Settings -> Privacy & Security -> Automation."
        )
    if "(-2741)" in raw:
        return (
            "AppleScript syntax/runtime bridge failed while talking to Music. "
            "Try opening Music once manually, then run `python3 playlist_cover_helper_web.py doctor`."
        )
    return joined[:2000]


def _as_quote(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _safe_filename(s: str) -> str:
    s = s.strip()
    s = re.sub(r"[^\w\-. ]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return (s or "playlist")[:120]


def _pid_hex_to_signed_int64(pid_hex: str) -> int:
    s = (pid_hex or "").strip().upper()
    if not re.fullmatch(r"[0-9A-F]{16}", s):
        raise ValueError(f"invalid playlist pid: {pid_hex}")
    v = int(s, 16)
    if v >= 1 << 63:
        v -= 1 << 64
    return v


def _detect_format_and_ext(img_path: Path) -> Tuple[int, str]:
    raw = img_path.read_bytes()
    if raw.startswith(b"\xFF\xD8\xFF"):
        return FMT_JPEG, "jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1A\n"):
        return FMT_PNG, "png"
    if raw.startswith(b"II*\x00") or raw.startswith(b"MM\x00*"):
        return FMT_TIFF, "tiff"
    ext = img_path.suffix.lower().lstrip(".")
    if ext in {"jpg", "jpeg"}:
        return FMT_JPEG, "jpeg"
    if ext == "png":
        return FMT_PNG, "png"
    if ext in {"tif", "tiff"}:
        return FMT_TIFF, "tiff"
    raise ValueError(f"unsupported image format: {img_path}")


def _image_size(img_path: Path) -> Tuple[int, int]:
    proc = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(img_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "sips failed")
    m_w = re.search(r"pixelWidth:\s*(\d+)", proc.stdout)
    m_h = re.search(r"pixelHeight:\s*(\d+)", proc.stdout)
    if not m_w or not m_h:
        raise RuntimeError(f"cannot parse image size: {proc.stdout}")
    return int(m_w.group(1)), int(m_h.group(1))


def _backup_artwork_db(db_path: Path) -> None:
    ts = time.strftime("%Y%m%d-%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.exists():
            dst = Path(str(src) + f".bak.{ts}")
            shutil.copy2(src, dst)


def _stop_music_stack() -> None:
    subprocess.run(["osascript", "-e", 'tell application "Music" to quit'], check=False)
    subprocess.run(["pkill", "-f", "AMPArtworkAgent"], check=False)
    time.sleep(0.8)


def _start_music() -> None:
    subprocess.run(["open", "-a", "Music"], check=False)


def _format_code_to_ext(fmt_code: Optional[int]) -> str:
    if fmt_code == FMT_PNG:
        return "png"
    if fmt_code == FMT_TIFF:
        return "tiff"
    return "jpeg"


def _image_mime(img_path: Path) -> str:
    try:
        raw = img_path.read_bytes()[:12]
    except Exception:
        return "application/octet-stream"
    if raw.startswith(b"\x89PNG\r\n\x1A\n"):
        return "image/png"
    if raw.startswith(b"II*\x00") or raw.startswith(b"MM\x00*"):
        return "image/tiff"
    if raw.startswith(b"\xFF\xD8\xFF"):
        return "image/jpeg"
    return "application/octet-stream"


def _playlist_cover_path(playlist: Playlist) -> Path:
    return OUT_DIR / f"{_safe_filename(playlist.name)}__{playlist.pid}.jpg"


def _find_artwork_file(hash_string: str, kind: int, cache_id: int, ext: str) -> Optional[Path]:
    h = hash_string.strip().upper()
    exact = ARTWORK_DIR / f"{h}_sk_{kind}_cid_{cache_id}.{ext}"
    if exact.exists():
        return exact

    patterns = (
        f"{h}_sk_{kind}_cid_{cache_id}.*",
        f"{h}_sk_{kind}_cid_*.*",
        f"{h}_sk_*_cid_*.*",
    )
    for pattern in patterns:
        matches = sorted(ARTWORK_DIR.glob(pattern))
        if matches:
            return matches[0]
    return None


def current_playlist_cover_info(playlist_pid: str) -> Tuple[bool, Dict[str, object] | str]:
    if not ARTWORK_DB.exists():
        return False, f"artwork DB not found: {ARTWORK_DB}"
    if not ARTWORK_DIR.exists():
        return False, f"artwork dir not found: {ARTWORK_DIR}"

    try:
        pid_int = _pid_hex_to_signed_int64(playlist_pid)
    except Exception as exc:
        return False, str(exc)

    con: Optional[sqlite3.Connection] = None
    try:
        con = sqlite3.connect(str(ARTWORK_DB))
        row = con.execute(
            """
            select
                s.ZKIND,
                i.ZKIND,
                i.ZHASHSTRING,
                c.ZFORMAT,
                c.ZWIDTH,
                c.ZHEIGHT,
                c.ZCACHEID
            from ZDATABASEITEMINFO d
            join ZSOURCEINFO s on s.Z_PK=d.ZSOURCEINFO
            join ZIMAGEINFO i on i.Z_PK=s.ZIMAGEINFO
            left join ZCACHEITEM c on c.ZIMAGEINFO=i.Z_PK
            where d.ZPERSISTENTID=?
            order by coalesce(c.ZWIDTH, 0) * coalesce(c.ZHEIGHT, 0) desc
            limit 1
            """,
            (pid_int,),
        ).fetchone()
    except Exception as exc:
        return False, f"preview lookup failed: {exc}"
    finally:
        if con is not None:
            con.close()

    if not row:
        return False, "playlist artwork row not found"

    source_kind, image_kind, hash_string, fmt_code, width, height, cache_id = row
    if not hash_string:
        return False, "playlist artwork hash is empty"

    kind = int(image_kind if image_kind is not None else source_kind or 0)
    cid = int(cache_id or 1)
    ext = _format_code_to_ext(int(fmt_code) if fmt_code is not None else None)
    art_path = _find_artwork_file(str(hash_string), kind, cid, ext)
    if not art_path:
        return False, f"artwork file not found for hash {hash_string}"

    return True, {
        "path": str(art_path),
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, f"Kind {kind}"),
        "width": int(width) if width else None,
        "height": int(height) if height else None,
        "hash": str(hash_string),
    }


def cover_file_info(img_path: Path) -> Dict[str, object]:
    info: Dict[str, object] = {"path": str(img_path)}
    try:
        width, height = _image_size(img_path)
        info["width"] = width
        info["height"] = height
    except Exception:
        info["width"] = None
        info["height"] = None
    return info


def inject_cover_via_artwork_db(
    playlist_pid: str,
    cover_path: Path,
    *,
    manage_music: bool = True,
    backup: bool = True,
) -> Tuple[bool, str]:
    if not ARTWORK_DB.exists():
        return False, f"artwork DB not found: {ARTWORK_DB}"
    if not ARTWORK_DIR.exists():
        return False, f"artwork dir not found: {ARTWORK_DIR}"
    if not cover_path.exists():
        return False, f"cover file not found: {cover_path}"

    try:
        pid_int = _pid_hex_to_signed_int64(playlist_pid)
        fmt_code, ext = _detect_format_and_ext(cover_path)
        w, h = _image_size(cover_path)
        sha = hashlib.sha256(cover_path.read_bytes()).hexdigest().upper()
    except Exception as exc:
        return False, str(exc)

    dst_artwork = ARTWORK_DIR / f"{sha}_sk_{KIND_CUSTOM}_cid_1.{ext}"

    if manage_music:
        _stop_music_stack()

    con: Optional[sqlite3.Connection] = None
    try:
        if backup:
            _backup_artwork_db(ARTWORK_DB)
        con = sqlite3.connect(str(ARTWORK_DB))
        cur = con.cursor()
        row = cur.execute(
            "select ZSOURCEINFO from ZDATABASEITEMINFO where ZPERSISTENTID=?",
            (pid_int,),
        ).fetchone()
        if not row:
            con.close()
            return False, f"playlist pid not found in artwork DB: {playlist_pid}"
        source_pk = int(row[0])

        srow = cur.execute(
            "select ZIMAGEINFO from ZSOURCEINFO where Z_PK=?",
            (source_pk,),
        ).fetchone()
        if not srow or srow[0] is None:
            con.close()
            return False, f"source row has no image info (source={source_pk})"
        imageinfo_pk = int(srow[0])

        crow = cur.execute(
            "select Z_PK from ZCACHEITEM where ZIMAGEINFO=?",
            (imageinfo_pk,),
        ).fetchone()
        if not crow:
            con.close()
            return False, f"cache row missing for image info {imageinfo_pk}"

        shutil.copy2(cover_path, dst_artwork)
        cur.execute(
            "update ZIMAGEINFO set ZHASHSTRING=?, ZKIND=? where Z_PK=?",
            (sha, KIND_CUSTOM, imageinfo_pk),
        )
        cur.execute(
            "update ZCACHEITEM set ZFORMAT=?, ZWIDTH=?, ZHEIGHT=? where ZIMAGEINFO=?",
            (fmt_code, float(w), float(h), imageinfo_pk),
        )
        cur.execute(
            "update ZSOURCEINFO set ZKIND=?, ZURL=NULL, ZSTOREID=NULL where Z_PK=?",
            (KIND_CUSTOM, source_pk),
        )
        con.commit()
        con.close()
    except Exception as exc:
        if con is not None:
            con.close()
        return False, f"DB inject failed: {exc}"
    finally:
        if manage_music:
            _start_music()

    return True, f"db-injected:{dst_artwork}"


def inject_covers_via_artwork_db(items: List[Tuple[Playlist, Path]]) -> List[Dict[str, object]]:
    if not items:
        return []
    if not ARTWORK_DB.exists():
        err = f"artwork DB not found: {ARTWORK_DB}"
        return [{"pid": p.pid, "name": p.name, "ok": False, "error": err} for p, _ in items]
    if not ARTWORK_DIR.exists():
        err = f"artwork dir not found: {ARTWORK_DIR}"
        return [{"pid": p.pid, "name": p.name, "ok": False, "error": err} for p, _ in items]

    results: List[Dict[str, object]] = []
    _stop_music_stack()
    try:
        _backup_artwork_db(ARTWORK_DB)
        for playlist, cover_path in items:
            ok, msg = inject_cover_via_artwork_db(
                playlist.pid,
                cover_path,
                manage_music=False,
                backup=False,
            )
            row: Dict[str, object] = {"pid": playlist.pid, "name": playlist.name, "ok": ok}
            if ok:
                row["message"] = msg
            else:
                row["error"] = msg
            results.append(row)
    finally:
        _start_music()
    return results


def fetch_playlists() -> Tuple[bool, List[Playlist] | str]:
    script = r'''
set oldDelims to AppleScript's text item delimiters
set AppleScript's text item delimiters to tab
tell application "Music"
    -- Some playlist subclasses don't support a direct `whose smart/special kind` filter.
    -- Enumerate all playlists, then filter defensively per item.
    set ps to every playlist
    set rows to {}
    repeat with p in ps
        set includeIt to true
        try
            if (smart of p) is true then set includeIt to false
        end try
        try
            if (special kind of p) is not none then set includeIt to false
        end try
        if includeIt then
            set pName to name of p
            set pId to persistent ID of p
            set tCount to count of tracks of p
            set hasArt to false
            if tCount > 0 then
                try
                    set t to track 1 of p
                    set hasArt to ((count of artworks of t) > 0)
                end try
            end if
            set end of rows to (pId & tab & pName & tab & (tCount as text) & tab & (hasArt as text))
        end if
    end repeat
    set AppleScript's text item delimiters to linefeed
    set outText to rows as text
end tell
set AppleScript's text item delimiters to oldDelims
return outText
'''
    ok, out = _run_osascript(script)
    if not ok:
        return False, out

    result: List[Playlist] = []
    if not out:
        return True, result
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        pid, name, tcount, has_art = parts
        try:
            tc = int(tcount)
        except ValueError:
            tc = 0
        result.append(
            Playlist(pid=pid, name=name, track_count=tc, first_has_art=has_art.lower() == "true")
        )
    result.sort(key=lambda p: p.name.lower())
    return True, result


def export_first_track_artwork(playlist_pid: str, output_file: Path) -> Tuple[bool, str]:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    pid = _as_quote(playlist_pid)
    out_path = _as_quote(str(output_file))
    script = f'''
tell application "Music"
    try
        set p to first playlist whose persistent ID is "{pid}"
    on error
        return "ERR|PLAYLIST_NOT_FOUND"
    end try
    if (count of tracks of p) is 0 then
        return "ERR|NO_TRACKS"
    end if
    set pickedTrack to missing value
    repeat with t in (tracks of p)
        try
            if (count of artworks of t) > 0 then
                set pickedTrack to t
                exit repeat
            end if
        end try
    end repeat
    if pickedTrack is missing value then
        return "ERR|NO_TRACK_WITH_ARTWORK"
    end if
    set rawData to data of artwork 1 of pickedTrack
end tell
set outFile to POSIX file "{out_path}"
set fRef to open for access outFile with write permission
try
    set eof fRef to 0
    write rawData to fRef
    close access fRef
on error errMsg number errNum
    try
        close access fRef
    end try
    return "ERR|WRITE_FAILED|" & errNum & "|" & errMsg
end try
return "OK"
'''
    ok, out = _run_osascript(script)
    if not ok:
        return False, out
    if out.startswith("OK"):
        return True, "OK"
    return False, out


def get_first_track_seed(playlist_pid: str) -> Tuple[bool, Tuple[str, str, str] | str]:
    pid = _as_quote(playlist_pid)
    script = f'''
tell application "Music"
    try
        set pl to first playlist whose persistent ID is "{pid}"
    on error
        return "ERR|PLAYLIST_NOT_FOUND"
    end try
    if (count of tracks of pl) is 0 then
        return "ERR|NO_TRACKS"
    end if
    set t to track 1 of pl
    set tName to ""
    set tArtist to ""
    set tAlbum to ""
    try
        set tName to name of t
    end try
    try
        set tArtist to artist of t
    end try
    try
        set tAlbum to album of t
    end try
    return tName & tab & tArtist & tab & tAlbum
end tell
'''
    ok, out = _run_osascript(script)
    if not ok:
        return False, out
    if out.startswith("ERR|"):
        return False, out
    parts = out.split("\t")
    while len(parts) < 3:
        parts.append("")
    return True, (parts[0].strip(), parts[1].strip(), parts[2].strip())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _score_result(result: dict, track: str, artist: str, album: str) -> int:
    score = 0
    rt = _norm(str(result.get("trackName", "")))
    ra = _norm(str(result.get("artistName", "")))
    rc = _norm(str(result.get("collectionName", "")))
    qt = _norm(track)
    qa = _norm(artist)
    qc = _norm(album)
    if qt and rt == qt:
        score += 6
    elif qt and qt in rt:
        score += 3
    if qa and ra == qa:
        score += 4
    elif qa and qa in ra:
        score += 2
    if qc and rc == qc:
        score += 3
    elif qc and qc in rc:
        score += 1
    if result.get("artworkUrl100"):
        score += 1
    return score


def _upgrade_artwork_url(url: str) -> str:
    # Typical Apple CDN URL patterns.
    url = re.sub(r"/\d+x\d+bb\.", "/1200x1200bb.", url)
    url = re.sub(r"\.\d+x\d+-\d+\.", ".1200x1200-75.", url)
    return url


def fetch_artwork_from_catalog(track: str, artist: str, album: str) -> Tuple[bool, bytes | str]:
    term_parts = [x for x in [track, artist, album] if x]
    if not term_parts:
        return False, "No track metadata to query catalog artwork."
    term = " ".join(term_parts)
    results: List[dict] = []
    for country in ("CN", "US"):
        params = urllib.parse.urlencode(
            {
                "term": term,
                "media": "music",
                "entity": "song",
                "limit": "25",
                "country": country,
            }
        )
        url = f"https://itunes.apple.com/search?{params}"
        try:
            with urllib.request.urlopen(url, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception:
            continue
        rows = payload.get("results") or []
        if isinstance(rows, list) and rows:
            results = [r for r in rows if isinstance(r, dict)]
            if results:
                break
    if not results:
        return False, "Catalog returned no match."

    best: Optional[dict] = None
    best_score = -1
    for r in results:
        if not isinstance(r, dict):
            continue
        sc = _score_result(r, track, artist, album)
        if sc > best_score:
            best_score = sc
            best = r
    if not best:
        return False, "No valid catalog result."

    art_url = str(best.get("artworkUrl100", "")).strip()
    if not art_url:
        return False, "Catalog result has no artwork URL."

    candidates = [_upgrade_artwork_url(art_url), art_url]
    last_err = ""
    for u in candidates:
        try:
            req = urllib.request.Request(
                u,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "image/*,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=12) as img_resp:
                data = img_resp.read()
            if data:
                return True, data
        except Exception as exc:
            last_err = str(exc)
            continue
    return False, f"Artwork download failed: {last_err or 'unknown error'}"


def export_cover_with_fallback(playlist_pid: str, output_file: Path) -> Tuple[bool, str]:
    ok, msg = export_first_track_artwork(playlist_pid, output_file)
    if ok:
        return True, "local-artwork"

    # Fallback for cloud-only artwork: fetch from Apple catalog by first track metadata.
    if "ERR|NO_TRACK_WITH_ARTWORK" not in msg:
        return False, msg

    ok_seed, seed_or_err = get_first_track_seed(playlist_pid)
    if not ok_seed:
        return False, f"{msg}; seed={seed_or_err}"
    track, artist, album = seed_or_err  # type: ignore[misc]
    ok_cat, data_or_err = fetch_artwork_from_catalog(track, artist, album)
    if not ok_cat:
        return False, f"{msg}; catalog={data_or_err}"
    output_file.write_bytes(data_or_err)  # type: ignore[arg-type]
    return True, "catalog-fallback"


def copy_image_to_clipboard(img_path: Path) -> Tuple[bool, str]:
    p = _as_quote(str(img_path))
    script = f'''
set the clipboard to (read POSIX file "{p}" as picture)
return "OK"
'''
    return _run_osascript(script)


def reveal_playlist(playlist_name: str) -> Tuple[bool, str]:
    p = _as_quote(playlist_name)
    script = f'''
tell application "Music"
    reveal user playlist "{p}"
    activate
end tell
return "OK"
'''
    return _run_osascript(script)


INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>CoverFix</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f1ea;
      --panel: #fffdf8;
      --panel-soft: rgba(255,253,248,.88);
      --ink: #1f2523;
      --muted: #66706c;
      --line: #d8d0c3;
      --line-strong: #b9ae9e;
      --accent: #0f766e;
      --accent-dark: #0b5953;
      --danger: #a33b2e;
      --button-bg: #fffaf1;
      --table-head-bg: #ebe5da;
      --table-head-ink: #4e5854;
      --image-bg: #ebe5da;
      --log-bg: rgba(31,37,35,.96);
      --log-ink: #f3efe7;
      --brand-link: #8fcfc8;
      --shadow: 0 18px 48px rgba(45, 38, 28, .12);
    }
    [data-theme="dark"] {
      color-scheme: dark;
      --bg: #0f1312;
      --panel: #171d1b;
      --panel-soft: rgba(23,29,27,.92);
      --ink: #e7ebe8;
      --muted: #96a39f;
      --line: #2a3431;
      --line-strong: #3a4743;
      --accent: #2aa79d;
      --accent-dark: #1e8a81;
      --danger: #c05848;
      --button-bg: #202826;
      --table-head-bg: #222b29;
      --table-head-ink: #c4cfcb;
      --image-bg: #242d2b;
      --log-bg: #101614;
      --log-ink: #e5ece8;
      --brand-link: #8fd7cf;
      --shadow: 0 18px 48px rgba(0, 0, 0, .35);
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(135deg, rgba(15,118,110,.09), transparent 36%),
        linear-gradient(315deg, rgba(163,59,46,.08), transparent 34%),
        var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input { font: inherit; }
    button {
      min-height: 36px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: var(--button-bg);
      color: var(--ink);
      padding: 7px 12px;
      cursor: pointer;
    }
    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: white;
    }
    button.danger {
      border-color: var(--danger);
      background: var(--danger);
      color: white;
      font-weight: 600;
    }
    button:hover:not(:disabled) { border-color: var(--accent); }
    button.primary:hover:not(:disabled) { background: var(--accent-dark); }
    button.danger:hover:not(:disabled) { background: #7e2d23; border-color: #7e2d23; }
    button:disabled {
      cursor: not-allowed;
      opacity: .52;
    }
    .shell {
      width: min(1440px, calc(100vw - 32px));
      height: calc(100vh - 32px);
      margin: 16px auto;
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr) 150px;
      gap: 12px;
    }
    .app-head {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
    }
    h1, h2, p { margin: 0; }
    h1 {
      font-size: 28px;
      line-height: 1;
      letter-spacing: 0;
    }
    .eyebrow {
      margin-bottom: 5px;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }
    .project-meta {
      font-size: 12px;
      color: var(--muted);
      text-transform: none;
    }
    .project-meta a {
      color: var(--accent-dark);
      text-decoration: none;
    }
    .project-meta a:hover {
      text-decoration: underline;
    }
    .toolbar, .pane, .log-panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      box-shadow: var(--shadow);
    }
    .head-tools {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .theme-toggle {
      min-height: 32px;
      padding: 6px 10px;
      font-size: 12px;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      padding: 10px;
    }
    .toolbar .spacer { flex: 1 1 auto; }
    .runtime-notice {
      display: none;
      width: 100%;
      border: 1px solid #d59d93;
      border-radius: 6px;
      background: #fff3ef;
      color: #7b2b21;
      padding: 8px 10px;
      font-size: 13px;
    }
    .runtime-notice.show { display: block; }
    .selection-count {
      color: var(--muted);
      font-size: 13px;
      padding: 0 6px;
    }
    .workspace {
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
      gap: 12px;
    }
    .pane {
      min-height: 0;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .pane-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
    }
    .pane-head h2 {
      font-size: 14px;
      letter-spacing: 0;
    }
    .table-scroll {
      min-height: 0;
      flex: 1 1 auto;
      overflow-y: auto;
      overflow-x: auto;
      scrollbar-gutter: stable;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }
    thead th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: var(--table-head-bg);
      color: var(--table-head-ink);
      border-bottom: 1px solid var(--line-strong);
      font-weight: 650;
    }
    .sort-btn {
      border: 0;
      padding: 0;
      margin: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      font-weight: 650;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
    }
    .sort-btn:hover { color: var(--accent-dark); }
    .sort-indicator {
      min-width: 10px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1;
    }
    th.tracks .sort-btn {
      width: 100%;
      justify-content: flex-end;
    }
    th, td {
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
    }
    th.check, td.check { width: 46px; text-align: center; }
    th.tracks, td.tracks { width: 74px; text-align: right; }
    th.art, td.art { width: 108px; text-align: center; }
    th.state, td.state { width: 120px; }
    td.name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    tbody tr { cursor: pointer; }
    tbody tr:hover { background: rgba(15,118,110,.07); }
    tbody tr.sel { background: rgba(15,118,110,.14); }
    input[type="checkbox"] {
      width: 16px;
      height: 16px;
      accent-color: var(--accent);
      cursor: pointer;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      border-radius: 999px;
      border: 1px solid var(--line);
      padding: 2px 7px;
      color: var(--muted);
      background: #fbf7ef;
      font-size: 12px;
    }
    .pill.ready {
      border-color: rgba(15,118,110,.35);
      color: var(--accent-dark);
      background: rgba(15,118,110,.09);
    }
    .preview-body {
      min-height: 0;
      overflow: hidden;
      padding: 12px;
      display: grid;
      gap: 12px;
    }
    .preview-card {
      display: grid;
      gap: 8px;
    }
    .preview-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      color: #3c4541;
      font-size: 13px;
      font-weight: 650;
    }
    .image-box {
      position: relative;
      aspect-ratio: 1 / 1;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: var(--image-bg);
    }
    .image-box img {
      width: 100%;
      height: 100%;
      display: block;
      object-fit: cover;
    }
    .empty {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      padding: 16px;
      color: var(--muted);
      text-align: center;
      font-size: 13px;
    }
    .meta {
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      word-break: break-all;
    }
    .log-panel {
      min-height: 0;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .log-head {
      padding: 8px 12px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }
    .log-body {
      min-height: 0;
      flex: 1;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 210px;
      overflow: hidden;
    }
    #log {
      margin: 0;
      min-height: 100%;
      overflow: auto;
      padding: 10px 12px;
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      background: var(--log-bg);
      color: var(--log-ink);
    }
    .log-brand {
      border-left: 1px solid rgba(230, 224, 210, 0.12);
      background: var(--log-bg);
      display: grid;
      align-content: center;
      justify-items: center;
      gap: 6px;
      padding: 8px 10px;
      overflow: hidden;
    }
    .log-brand-image {
      width: 64px;
      height: 64px;
      object-fit: cover;
      border-radius: 999px;
      border: 1px solid rgba(230, 224, 210, 0.35);
      background: #0e1110;
    }
    .log-brand-title {
      color: var(--log-ink);
      font-size: 11px;
      font-weight: 650;
      text-align: center;
      line-height: 1.25;
    }
    .log-brand-meta {
      color: #c8c0b2;
      font-size: 11px;
      text-align: center;
      line-height: 1.2;
      white-space: nowrap;
    }
    .log-brand-meta a {
      color: var(--brand-link);
      text-decoration: none;
    }
    .log-brand-meta a:hover { text-decoration: underline; }
    @media (max-width: 900px) {
      .shell {
        width: calc(100vw - 20px);
        height: auto;
        min-height: calc(100vh - 20px);
        margin: 10px auto;
        grid-template-rows: auto auto auto 150px;
      }
      .workspace { grid-template-columns: 1fr; }
      .preview-body { grid-template-columns: 1fr 1fr; }
      .app-head { align-items: start; flex-direction: column; }
      .log-body { grid-template-columns: 1fr; }
      .log-brand { display: none; }
    }
    @media (max-width: 560px) {
      .preview-body { grid-template-columns: 1fr; }
      th.art, td.art, th.state, td.state { display: none; }
      h1 { font-size: 24px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="app-head">
      <div>
        <p class="eyebrow">Apple Music local artwork</p>
        <h1>CoverFix</h1>
      </div>
      <div class="head-tools">
        <button id="btnTheme" class="theme-toggle" type="button" onclick="toggleTheme()">Dark Mode</button>
      </div>
    </header>

    <section class="toolbar">
      <button id="btnRefresh" onclick="loadPlaylists()">Refresh</button>
      <button id="btnSelectAll" onclick="selectAll()">Select All</button>
      <button id="btnClear" onclick="clearSelection()">Clear</button>
      <span id="selectionCount" class="selection-count">0 selected</span>
      <span class="spacer"></span>
      <button id="btnGen" onclick="generateSelected()">Generate Selected</button>
      <button id="btnApply" class="danger" onclick="applySelected()">Apply</button>
      <button id="btnOpen" onclick="openCovers()">Open Folder</button>
      <div id="runtimeNotice" class="runtime-notice"></div>
    </section>

    <main class="workspace">
      <section class="pane">
        <div class="table-scroll">
          <table id="tbl">
            <thead>
              <tr>
                <th class="check"></th>
                <th>
                  <button id="sortPlaylistBtn" class="sort-btn" type="button" onclick="toggleSort('name')">
                    Playlist
                    <span id="sortPlaylistIndicator" class="sort-indicator">↑</span>
                  </button>
                </th>
                <th class="tracks">
                  <button id="sortTracksBtn" class="sort-btn" type="button" onclick="toggleSort('tracks')">
                    Tracks
                    <span id="sortTracksIndicator" class="sort-indicator">↕</span>
                  </button>
                </th>
                <th class="art">Track Art</th>
                <th class="state">Candidate</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
          <div id="emptyState" hidden style="padding:24px 16px;color:var(--muted);font-size:.9em;line-height:1.6">
            No playlists found.<br>
            Make sure Apple Music is running and has regular (non-smart) user playlists.<br>
            Check the Log panel below for the exact error.
          </div>
        </div>
      </section>

      <aside class="pane">
        <div class="pane-head">
          <h2>Preview</h2>
          <span id="previewName" class="selection-count">No selection</span>
        </div>
        <div class="preview-body">
          <div class="preview-card">
            <div class="preview-title"><span>Current in Music</span></div>
            <div class="image-box">
              <img id="currentImg" alt="Current playlist cover" hidden/>
              <div id="currentEmpty" class="empty">No playlist selected</div>
            </div>
            <div id="currentMeta" class="meta"></div>
          </div>
          <div class="preview-card">
            <div class="preview-title"><span>Generated Candidate</span></div>
            <div class="image-box">
              <img id="generatedImg" alt="Generated playlist cover" hidden/>
              <div id="generatedEmpty" class="empty">No generated cover</div>
            </div>
            <div id="generatedMeta" class="meta"></div>
          </div>
        </div>
      </aside>
    </main>

    <section class="log-panel">
      <div class="log-head">Log</div>
      <div class="log-body">
        <pre id="log"></pre>
        <aside class="log-brand">
          <img class="log-brand-image" src="/api/brand-image" alt="Evil Panda MD Production mark" loading="lazy" />
          <div class="log-brand-title">Evil Panda MD Production</div>
          <div class="log-brand-meta">
            <a href="__GITHUB_URL__" target="_blank" rel="noopener noreferrer">GitHub</a>
            · MIT License
          </div>
        </aside>
      </div>
    </section>
  </div>
<script>
const RUNTIME = __RUNTIME_JSON__;
let playlists = [];
let covers = {};
let selected = new Set();
let isRefreshing = false;
let isWorking = false;
let previewSeq = 0;
let previewPid = null;
let sortKey = "name";
let sortDir = "asc";

const tbody = document.querySelector("#tbl tbody");
const logEl = document.getElementById("log");
const btnTheme = document.getElementById("btnTheme");
const btnRefresh = document.getElementById("btnRefresh");
const btnSelectAll = document.getElementById("btnSelectAll");
const btnClear = document.getElementById("btnClear");
const btnGen = document.getElementById("btnGen");
const btnApply = document.getElementById("btnApply");
const btnOpen = document.getElementById("btnOpen");
const runtimeNotice = document.getElementById("runtimeNotice");
const emptyState = document.getElementById("emptyState");
const selectionCount = document.getElementById("selectionCount");
const sortPlaylistIndicator = document.getElementById("sortPlaylistIndicator");
const sortTracksIndicator = document.getElementById("sortTracksIndicator");
const previewName = document.getElementById("previewName");
const currentImg = document.getElementById("currentImg");
const currentEmpty = document.getElementById("currentEmpty");
const currentMeta = document.getElementById("currentMeta");
const generatedImg = document.getElementById("generatedImg");
const generatedEmpty = document.getElementById("generatedEmpty");
const generatedMeta = document.getElementById("generatedMeta");

function savedTheme(){
  try {
    return localStorage.getItem("coverfix-theme");
  } catch {
    return null;
  }
}
function preferredTheme(){
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(mode){
  const normalized = mode === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", normalized);
  btnTheme.textContent = normalized === "dark" ? "Light Mode" : "Dark Mode";
}
function toggleTheme(){
  const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  const next = current === "dark" ? "light" : "dark";
  applyTheme(next);
  try {
    localStorage.setItem("coverfix-theme", next);
  } catch {}
}

function log(msg){
  logEl.textContent += msg + "\\n";
  logEl.scrollTop = logEl.scrollHeight;
}
function setStatus(_s){ }
function selectedIds(){ return playlists.filter(p => selected.has(p.pid)).map(p => p.pid); }
function plural(n, word, many){
  return n + " " + (n === 1 ? word : (many || word + "s"));
}
function sortIndicator(key){
  if(sortKey !== key) return "↕";
  return sortDir === "asc" ? "↑" : "↓";
}
function updateSortIndicators(){
  sortPlaylistIndicator.textContent = sortIndicator("name");
  sortTracksIndicator.textContent = sortIndicator("tracks");
}
function sortedPlaylists(){
  const dir = sortDir === "asc" ? 1 : -1;
  const rows = [...playlists];
  rows.sort((a, b) => {
    if(sortKey === "tracks"){
      const delta = (a.track_count - b.track_count) * dir;
      if(delta !== 0) return delta;
      return a.name.localeCompare(b.name, undefined, {sensitivity: "base"});
    }
    const byName = a.name.localeCompare(b.name, undefined, {sensitivity: "base"}) * dir;
    if(byName !== 0) return byName;
    return (a.track_count - b.track_count);
  });
  return rows;
}
function toggleSort(key){
  if(sortKey === key){
    sortDir = sortDir === "asc" ? "desc" : "asc";
  } else {
    sortKey = key;
    sortDir = key === "tracks" ? "desc" : "asc";
  }
  renderPlaylists();
}
function sizeText(info){
  return info && info.width && info.height ? info.width + "x" + info.height : "";
}
function setRefreshingBusy(b){
  isRefreshing = b;
  btnRefresh.disabled = b || isWorking;
  btnRefresh.textContent = b ? "Refreshing..." : "Refresh";
}
function setWorking(b, label){
  isWorking = b;
  btnGen.textContent = b ? label : "Generate Selected";
  btnApply.textContent = b ? label : "Apply";
  updateActionState();
}
function runtimeUnsupported(){
  return !RUNTIME.supported;
}
function showRuntimeNotice(){
  if(runtimeUnsupported()){
    runtimeNotice.classList.add("show");
    runtimeNotice.textContent = RUNTIME.reason || "Unsupported runtime";
    setStatus(runtimeNotice.textContent);
    log("[WARN] " + runtimeNotice.textContent);
  } else {
    runtimeNotice.classList.remove("show");
  }
}
function updateActionState(){
  const count = selected.size;
  selectionCount.textContent = plural(count, "selected", "selected");
  btnGen.disabled = runtimeUnsupported() || count === 0 || isWorking || isRefreshing;
  btnApply.disabled = runtimeUnsupported() || count === 0 || isWorking || isRefreshing;
  btnSelectAll.disabled = playlists.length === 0 || isWorking || isRefreshing;
  btnClear.disabled = count === 0 || isWorking || isRefreshing;
  btnRefresh.disabled = runtimeUnsupported() || isWorking || isRefreshing;
  btnOpen.disabled = runtimeUnsupported();
}
function cell(text, className){
  const td = document.createElement("td");
  if(className) td.className = className;
  td.textContent = text;
  return td;
}
function badge(text, ready=false){
  const span = document.createElement("span");
  span.className = ready ? "pill ready" : "pill";
  span.textContent = text;
  return span;
}
function renderPlaylists(){
  const valid = new Set(playlists.map(p => p.pid));
  for(const pid of [...selected]){
    if(!valid.has(pid)) selected.delete(pid);
  }
  updateSortIndicators();
  tbody.textContent = "";
  emptyState.hidden = playlists.length > 0;
  for(const p of sortedPlaylists()){
    const tr = document.createElement("tr");
    tr.dataset.pid = p.pid;
    tr.classList.toggle("sel", selected.has(p.pid));

    const checkCell = document.createElement("td");
    checkCell.className = "check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = selected.has(p.pid);
    cb.setAttribute("aria-label", "Select " + p.name);
    cb.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleSelection(p.pid, cb.checked);
    });
    checkCell.appendChild(cb);
    tr.appendChild(checkCell);

    tr.appendChild(cell(p.name, "name"));
    tr.appendChild(cell(String(p.track_count), "tracks"));
    const artCell = document.createElement("td");
    artCell.className = "art";
    artCell.appendChild(badge(p.first_has_art ? "yes" : "no", p.first_has_art));
    tr.appendChild(artCell);
    const stateCell = document.createElement("td");
    stateCell.className = "state";
    stateCell.appendChild(badge(covers[p.pid] ? "ready" : "none", Boolean(covers[p.pid])));
    tr.appendChild(stateCell);

    tr.addEventListener("click", () => toggleSelection(p.pid, !selected.has(p.pid)));
    tr.addEventListener("mouseenter", () => {
      previewPid = p.pid;
      refreshPreview();
    });
    tr.addEventListener("mouseleave", () => {
      if(previewPid === p.pid){
        previewPid = null;
        refreshPreview();
      }
    });
    tbody.appendChild(tr);
  }
  updateActionState();
  refreshPreview();
}
function toggleSelection(pid, checked){
  if(checked) selected.add(pid);
  else selected.delete(pid);
  const row = tbody.querySelector('tr[data-pid="' + pid + '"]');
  if(row){
    row.classList.toggle("sel", checked);
    const cb = row.querySelector('input[type="checkbox"]');
    if(cb) cb.checked = checked;
  }
  updateActionState();
  refreshPreview();
}
function selectAll(){
  playlists.forEach(p => selected.add(p.pid));
  renderPlaylists();
}
function clearSelection(){
  selected.clear();
  renderPlaylists();
}
function resetImage(img, empty, text){
  img.hidden = true;
  img.removeAttribute("src");
  empty.hidden = false;
  empty.textContent = text;
}
function loadImage(img, empty, src, emptyText){
  img.hidden = true;
  empty.hidden = false;
  empty.textContent = "Loading...";
  img.onload = () => {
    img.hidden = false;
    empty.hidden = true;
  };
  img.onerror = () => {
    img.hidden = true;
    empty.hidden = false;
    empty.textContent = emptyText;
  };
  img.src = src;
}
async function refreshPreview(){
  const ids = selectedIds();
  const activePid = previewPid || (ids.length === 1 ? ids[0] : null);
  const seq = ++previewSeq;
  currentMeta.textContent = "";
  generatedMeta.textContent = "";
  if(!activePid){
    previewName.textContent = ids.length ? plural(ids.length, "selected", "selected") : "No selection";
    resetImage(currentImg, currentEmpty, ids.length ? "Select one playlist to preview" : "No playlist selected");
    resetImage(generatedImg, generatedEmpty, ids.length ? "Select one playlist to preview" : "No generated cover");
    return;
  }

  const pid = activePid;
  const playlist = playlists.find(p => p.pid === pid);
  previewName.textContent = playlist ? playlist.name : "Preview";
  const stamp = Date.now();
  loadImage(currentImg, currentEmpty, "/api/image?kind=current&pid=" + encodeURIComponent(pid) + "&v=" + stamp, "No current cover found");
  loadImage(generatedImg, generatedEmpty, "/api/image?kind=generated&pid=" + encodeURIComponent(pid) + "&v=" + stamp, "No generated cover");

  try {
    const d = await api("/api/cover-info?pid=" + encodeURIComponent(pid), {}, 12000);
    if(seq !== previewSeq) return;
    if(d.current && d.current.ok){
      const size = sizeText(d.current);
      currentMeta.textContent = [d.current.kind_label, size, d.current.path].filter(Boolean).join(" - ");
    } else {
      currentMeta.textContent = d.current?.error || "";
    }
    if(d.generated && d.generated.ok){
      const size = sizeText(d.generated);
      generatedMeta.textContent = [size, d.generated.path].filter(Boolean).join(" - ");
    } else {
      generatedMeta.textContent = d.generated?.error || "";
    }
  } catch (e) {
    if(seq === previewSeq) currentMeta.textContent = "Preview lookup failed";
  }
}
async function api(path, opts={}, timeoutMs=30000){
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(path, {...opts, signal: ctrl.signal});
    const text = await r.text();
    try {
      return JSON.parse(text);
    } catch {
      return {ok:false, error:text || r.statusText};
    }
  } finally {
    clearTimeout(t);
  }
}
async function loadPlaylists(){
  if(runtimeUnsupported()){
    setRefreshingBusy(false);
    showRuntimeNotice();
    return;
  }
  if(isRefreshing){
    log("[INFO] Refresh already in progress...");
    return;
  }
  setRefreshingBusy(true);
  setStatus("Refreshing playlists...");
  log("Refreshing playlists...");
  try {
    const d = await api("/api/playlists", {}, 45000);
    if(!d.ok){
      log("[ERROR] " + d.error);
      setStatus("Error: " + d.error);
      return;
    }
    playlists = d.playlists || [];
    renderPlaylists();
    log("Loaded " + playlists.length + " playlists.");
    setStatus("Loaded " + playlists.length + " playlists.");
  } catch (e) {
    log("[ERROR] refresh request failed: " + (e?.message || e));
    setStatus("Error: " + (e?.message || "refresh failed"));
  } finally {
    setRefreshingBusy(false);
    updateActionState();
  }
}
function mergeResults(d, mode){
  const results = d.results || [];
  if(!results.length && d.error){
    log("[ERROR] " + mode + ": " + d.error);
  }
  let okCount = 0;
  let failCount = 0;
  for(const r of results){
    if(r.ok){
      okCount += 1;
      if(r.cover_file) covers[r.pid] = r.cover_file;
      const source = r.source ? " (" + r.source + ")" : "";
      log("[OK] " + mode + ": " + r.name + source);
    } else {
      failCount += 1;
      log("[ERROR] " + mode + ": " + (r.name || r.pid) + " - " + (r.error || "unknown error"));
    }
  }
  setStatus(mode + ": " + okCount + " ok, " + failCount + " failed");
}
async function generateSelected(){
  const ids = selectedIds();
  if(!ids.length){ return; }
  setWorking(true, "Generating...");
  setStatus("Generating " + ids.length + " cover candidates...");
  try {
    const d = await api("/api/generate", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({pids:ids})
    }, Math.max(60000, ids.length * 20000));
    mergeResults(d, "Generated");
    renderPlaylists();
  } catch (e) {
    log("[ERROR] generate request failed: " + (e?.message || e));
    setStatus("Generate failed");
  } finally {
    setWorking(false, "Generate Selected");
  }
}
async function applySelected(){
  const ids = selectedIds();
  if(!ids.length){ return; }
  const question =
    "Apply covers to " + ids.length + " playlist(s) and restart Apple Music once after batch?\\n\\n" +
    "This modifies Apple Music artwork database.";
  if(!confirm(question)){ return; }
  setWorking(true, "Applying...");
  setStatus("Applying " + ids.length + " playlists...");
  try {
    const d = await api("/api/apply", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({pids:ids})
    }, Math.max(90000, ids.length * 35000));
    mergeResults(d, "Applied");
    if(d.restarted){
      log("[INFO] Music restarted once after the batch.");
    }
    renderPlaylists();
    await loadPlaylists();
  } catch (e) {
    log("[ERROR] apply request failed: " + (e?.message || e));
    setStatus("Apply failed");
  } finally {
    setWorking(false, "Apply");
  }
}
function openCovers(){ window.open("/api/open-covers"); }
showRuntimeNotice();
updateActionState();
applyTheme(savedTheme() || preferredTheme());
loadPlaylists();
</script>
</body>
</html>
"""


class State:
    playlists: List[Playlist] = []
    covers: Dict[str, Path] = {}

    @classmethod
    def by_id(cls) -> Dict[str, Playlist]:
        return {p.pid: p for p in cls.playlists}


def generated_cover_path(pid: str) -> Optional[Path]:
    cover = State.covers.get(pid)
    if cover and cover.exists():
        return cover
    matches: List[Path] = []
    for folder in (OUT_DIR, LEGACY_OUT_DIR):
        if folder.exists():
            matches.extend(folder.glob(f"*__{pid}.*"))
    matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for match in matches:
        if match.is_file():
            State.covers[pid] = match
            return match
    return None


def render_index_html() -> str:
    html = INDEX_HTML.replace("__RUNTIME_JSON__", json.dumps(runtime_info(), ensure_ascii=False))
    return html.replace("__GITHUB_URL__", APP_GITHUB_URL)


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, html: str, status: int = 200) -> None:
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send_image(self, img_path: Path) -> None:
        if not img_path.exists() or not img_path.is_file():
            self._json({"ok": False, "error": "image not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", _image_mime(img_path))
        self.send_header("Content-Length", str(img_path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with img_path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def _load_playlists_if_needed(self) -> Tuple[bool, str]:
        ok_rt, rt_msg = require_supported_runtime()
        if not ok_rt:
            return False, rt_msg
        if State.playlists:
            return True, "OK"
        ok, result = fetch_playlists()
        if not ok:
            return False, str(result)
        State.playlists = result  # type: ignore[assignment]
        return True, "OK"

    def _requested_playlists(self, data: dict) -> Tuple[Optional[List[Playlist]], Optional[Tuple[dict, int]]]:
        pids: List[str] = []
        raw_pids = data.get("pids")
        if isinstance(raw_pids, list):
            for item in raw_pids:
                if isinstance(item, str) and item not in pids:
                    pids.append(item)
        elif isinstance(data.get("pid"), str):
            pids.append(data["pid"])

        if not pids:
            return None, ({"ok": False, "error": "pid or pids required"}, 400)

        ok, msg = self._load_playlists_if_needed()
        if not ok:
            return None, ({"ok": False, "error": msg}, 500)

        by_id = State.by_id()
        missing = [pid for pid in pids if pid not in by_id]
        if missing:
            ok_fetch, result = fetch_playlists()
            if ok_fetch:
                State.playlists = result  # type: ignore[assignment]
                by_id = State.by_id()

        playlists = [by_id[pid] for pid in pids if pid in by_id]
        if not playlists:
            return None, ({"ok": False, "error": "playlist not found"}, 404)
        return playlists, None

    def _generate_for_playlists(self, playlists: List[Playlist]) -> List[Dict[str, object]]:
        results: List[Dict[str, object]] = []
        for playlist in playlists:
            cover_path = _playlist_cover_path(playlist)
            ok, msg = export_cover_with_fallback(playlist.pid, cover_path)
            row: Dict[str, object] = {
                "pid": playlist.pid,
                "name": playlist.name,
                "ok": ok,
            }
            if ok:
                State.covers[playlist.pid] = cover_path
                row["cover_file"] = str(cover_path)
                row["source"] = msg
            else:
                row["error"] = msg
            results.append(row)
        return results

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if p == "/":
            self._html(render_index_html())
            return
        if p == "/api/runtime":
            self._json({"ok": True, "runtime": runtime_info()})
            return
        if p == "/api/playlists":
            ok_rt, rt_msg = require_supported_runtime()
            if not ok_rt:
                self._json({"ok": False, "error": rt_msg}, 400)
                return
            ok, result = fetch_playlists()
            if not ok:
                self._json({"ok": False, "error": result}, 500)
                return
            State.playlists = result  # type: ignore[assignment]
            self._json({"ok": True, "playlists": [asdict(x) for x in State.playlists]})
            return
        if p == "/api/cover-info":
            ok_rt, rt_msg = require_supported_runtime()
            if not ok_rt:
                self._json({"ok": False, "error": rt_msg}, 400)
                return
            pid = (qs.get("pid") or [""])[0]
            if not pid:
                self._json({"ok": False, "error": "pid required"}, 400)
                return
            ok_current, current = current_playlist_cover_info(pid)
            generated = generated_cover_path(pid)
            self._json(
                {
                    "ok": True,
                    "current": {"ok": True, **current} if ok_current else {"ok": False, "error": current},
                    "generated": (
                        {"ok": True, **cover_file_info(generated)}
                        if generated
                        else {"ok": False, "error": "generated cover not found"}
                    ),
                }
            )
            return
        if p == "/api/image":
            ok_rt, rt_msg = require_supported_runtime()
            if not ok_rt:
                self._json({"ok": False, "error": rt_msg}, 400)
                return
            pid = (qs.get("pid") or [""])[0]
            kind = (qs.get("kind") or [""])[0]
            if not pid or kind not in {"current", "generated"}:
                self._json({"ok": False, "error": "pid and valid kind required"}, 400)
                return
            if kind == "current":
                ok_current, current = current_playlist_cover_info(pid)
                if not ok_current:
                    self._json({"ok": False, "error": current}, 404)
                    return
                self._send_image(Path(str(current["path"])))  # type: ignore[index]
                return
            generated = generated_cover_path(pid)
            if not generated:
                self._json({"ok": False, "error": "generated cover not found"}, 404)
                return
            self._send_image(generated)
            return
        if p == "/api/brand-image":
            self._send_image(APP_BRAND_IMAGE_PATH)
            return
        if p == "/api/open-covers":
            ok_rt, rt_msg = require_supported_runtime()
            if not ok_rt:
                self._json({"ok": False, "error": rt_msg}, 400)
                return
            subprocess.run(["open", str(OUT_DIR)], check=False)
            self._json({"ok": True})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        p = urllib.parse.urlparse(self.path).path
        ok_rt, rt_msg = require_supported_runtime()
        if not ok_rt:
            self._json({"ok": False, "error": rt_msg}, 400)
            return
        data = self._read_json()
        playlists, err = self._requested_playlists(data)
        if err:
            payload, status = err
            self._json(payload, status)
            return
        assert playlists is not None

        if p == "/api/generate":
            results = self._generate_for_playlists(playlists)
            succeeded = sum(1 for r in results if r["ok"])
            self._json(
                {
                    "ok": succeeded > 0,
                    "succeeded": succeeded,
                    "failed": len(results) - succeeded,
                    "results": results,
                },
                200 if succeeded else 500,
            )
            return

        if p == "/api/apply":
            generated = self._generate_for_playlists(playlists)
            generated_by_pid = {str(r["pid"]): r for r in generated}
            prepared: List[Tuple[Playlist, Path]] = []
            for playlist in playlists:
                row = generated_by_pid.get(playlist.pid)
                if row and row["ok"] and row.get("cover_file"):
                    prepared.append((playlist, Path(str(row["cover_file"]))))

            applied_by_pid: Dict[str, Dict[str, object]] = {}
            restarted = False
            if prepared:
                applied = inject_covers_via_artwork_db(prepared)
                restarted = True
                applied_by_pid = {str(r["pid"]): r for r in applied}

            results: List[Dict[str, object]] = []
            for playlist in playlists:
                gen_row = generated_by_pid.get(playlist.pid)
                if not gen_row or not gen_row["ok"]:
                    results.append(
                        {
                            "pid": playlist.pid,
                            "name": playlist.name,
                            "ok": False,
                            "error": gen_row.get("error", "generate failed") if gen_row else "generate failed",
                        }
                    )
                    continue

                apply_row = applied_by_pid.get(playlist.pid)
                if apply_row and apply_row["ok"]:
                    State.covers[playlist.pid] = Path(str(gen_row["cover_file"]))
                    results.append(
                        {
                            "pid": playlist.pid,
                            "name": playlist.name,
                            "ok": True,
                            "cover_file": gen_row["cover_file"],
                            "source": gen_row.get("source"),
                            "apply_message": apply_row.get("message"),
                        }
                    )
                else:
                    results.append(
                        {
                            "pid": playlist.pid,
                            "name": playlist.name,
                            "ok": False,
                            "cover_file": gen_row.get("cover_file"),
                            "source": gen_row.get("source"),
                            "error": apply_row.get("error", "apply failed") if apply_row else "apply failed",
                        }
                    )

            succeeded = sum(1 for r in results if r["ok"])
            self._json(
                {
                    "ok": succeeded > 0,
                    "succeeded": succeeded,
                    "failed": len(results) - succeeded,
                    "restarted": restarted,
                    "results": results,
                },
                200 if succeeded else 500,
            )
            return

        self._json({"ok": False, "error": "not found"}, 404)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _backup_timestamps() -> List[str]:
    base_pattern = f"{ARTWORK_DB.name}.bak.*"
    timestamps: List[str] = []
    for path in sorted(ARTWORK_DB.parent.glob(base_pattern)):
        m = re.search(r"\.bak\.(\d{8}-\d{6})$", path.name)
        if m:
            timestamps.append(m.group(1))
    return sorted(set(timestamps))


def list_backups() -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for ts in _backup_timestamps():
        row: Dict[str, object] = {"timestamp": ts, "files": []}
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(ARTWORK_DB) + suffix + f".bak.{ts}")
            if src.exists():
                row["files"].append(str(src))
        out.append(row)
    return out


def restore_backup(timestamp: str, restart_music: bool = True) -> Tuple[bool, str]:
    base_src = Path(str(ARTWORK_DB) + f".bak.{timestamp}")
    if not base_src.exists():
        return False, f"backup timestamp not found: {timestamp}"

    _stop_music_stack()
    restored: List[str] = []
    try:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(ARTWORK_DB) + suffix + f".bak.{timestamp}")
            dst = Path(str(ARTWORK_DB) + suffix)
            if src.exists():
                shutil.copy2(src, dst)
                restored.append(str(dst))
    finally:
        if restart_music:
            _start_music()
    return True, f"restored {len(restored)} file(s): {', '.join(restored)}"


def _resolve_playlists_by_pids(pids: List[str]) -> Tuple[bool, List[Playlist] | str]:
    ok, result = fetch_playlists()
    if not ok:
        return False, str(result)
    by_id = {p.pid: p for p in result}
    rows = [by_id[pid] for pid in pids if pid in by_id]
    missing = [pid for pid in pids if pid not in by_id]
    if missing:
        return False, f"playlist not found for pid(s): {', '.join(missing)}"
    return True, rows


def cli_list_playlists() -> int:
    ok_rt, rt_msg = require_supported_runtime()
    if not ok_rt:
        print(f"[ERROR] {rt_msg}")
        return 2
    ok, result = fetch_playlists()
    if not ok:
        print(f"[ERROR] {result}")
        return 1
    for p in result:
        print(f"{p.pid}\t{p.track_count}\t{p.name}")
    return 0


def cli_generate(pids: List[str]) -> int:
    ok_rt, rt_msg = require_supported_runtime()
    if not ok_rt:
        print(f"[ERROR] {rt_msg}")
        return 2
    ok, rows_or_err = _resolve_playlists_by_pids(pids)
    if not ok:
        print(f"[ERROR] {rows_or_err}")
        return 1
    assert isinstance(rows_or_err, list)
    results: List[Dict[str, object]] = []
    for playlist in rows_or_err:
        cover_path = _playlist_cover_path(playlist)
        ok_gen, msg = export_cover_with_fallback(playlist.pid, cover_path)
        if ok_gen:
            results.append(
                {
                    "pid": playlist.pid,
                    "name": playlist.name,
                    "ok": True,
                    "cover_file": str(cover_path),
                    "source": msg,
                }
            )
        else:
            results.append({"pid": playlist.pid, "name": playlist.name, "ok": False, "error": msg})
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0 if all(bool(r.get("ok")) for r in results) else 1


def cli_apply(pids: List[str]) -> int:
    ok_rt, rt_msg = require_supported_runtime()
    if not ok_rt:
        print(f"[ERROR] {rt_msg}")
        return 2
    ok, rows_or_err = _resolve_playlists_by_pids(pids)
    if not ok:
        print(f"[ERROR] {rows_or_err}")
        return 1
    assert isinstance(rows_or_err, list)
    prepared: List[Tuple[Playlist, Path]] = []
    for playlist in rows_or_err:
        cover_path = _playlist_cover_path(playlist)
        ok_gen, msg = export_cover_with_fallback(playlist.pid, cover_path)
        if not ok_gen:
            print(f"[ERROR] generate failed for {playlist.name}: {msg}")
            continue
        prepared.append((playlist, cover_path))
    if not prepared:
        print("[ERROR] no playlist prepared for apply")
        return 1
    results = inject_covers_via_artwork_db(prepared)
    print(json.dumps({"results": results, "restarted": True}, ensure_ascii=False, indent=2))
    return 0 if all(bool(r.get("ok")) for r in results) else 1


def cli_doctor() -> int:
    report: Dict[str, object] = {"app": APP_TITLE, "runtime": runtime_info()}
    checks: Dict[str, object] = {}
    checks["artwork_db_exists"] = ARTWORK_DB.exists()
    checks["artwork_dir_exists"] = ARTWORK_DIR.exists()
    checks["out_dir"] = str(OUT_DIR)
    checks["osascript_exists"] = shutil.which("osascript") is not None
    if checks["osascript_exists"]:
        ok, out = _run_osascript('tell application "Music" to get name')
        checks["music_applescript_ok"] = ok
        checks["music_applescript_result"] = out if ok else str(out)
        ok_list, list_out = fetch_playlists()
        checks["fetch_playlists_ok"] = ok_list
        checks["fetch_playlists_result"] = (
            f"{len(list_out)} playlist(s)" if ok_list else str(list_out)
        )
    else:
        checks["music_applescript_ok"] = False
        checks["music_applescript_result"] = "osascript not found"
        checks["fetch_playlists_ok"] = False
        checks["fetch_playlists_result"] = "osascript not found"
    report["checks"] = checks
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if bool(checks.get("music_applescript_ok")) and bool(checks.get("fetch_playlists_ok")) else 1


def run_web(open_browser: bool = True) -> int:
    url = f"http://{HOST}:{PORT}/"
    rt = runtime_info()
    if not bool(rt["supported"]):
        print(f"[WARN] {rt['reason']}")
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        if getattr(exc, "errno", None) == 48:
            print(
                f"[ERROR] Port {PORT} is already in use.\n"
                f"Stop the existing process, or open {url} if CoverFix is already running."
            )
            return 1
        raise
    print(f"{APP_TITLE} running at {url}")

    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        server.server_close()
        print("Server stopped cleanly.")
    return 0


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CoverFix for Apple Music playlist covers.")
    sub = parser.add_subparsers(dest="command")

    p_web = sub.add_parser("web", help="Run web UI server")
    p_web.add_argument("--no-open", action="store_true", help="Do not open browser automatically")

    sub.add_parser("list", help="List playlists (pid, track_count, name)")

    p_gen = sub.add_parser("generate", help="Generate cover file(s) for playlist pid(s)")
    p_gen.add_argument("--pid", action="append", required=True, help="Playlist persistent ID (repeatable)")

    p_apply = sub.add_parser("apply", help="Generate and apply cover(s) for playlist pid(s)")
    p_apply.add_argument("--pid", action="append", required=True, help="Playlist persistent ID (repeatable)")

    p_rescue = sub.add_parser("rescue", help="Rescue mode: list/restore artwork DB backups")
    p_rescue.add_argument("--list", action="store_true", help="List available backups")
    p_rescue.add_argument("--latest", action="store_true", help="Restore latest backup")
    p_rescue.add_argument("--timestamp", help="Restore specific timestamp (YYYYMMDD-HHMMSS)")
    p_rescue.add_argument("--yes", action="store_true", help="Confirm destructive restore action")
    p_rescue.add_argument("--no-restart", action="store_true", help="Do not re-open Music after restore")

    sub.add_parser("doctor", help="Run diagnostics for AppleScript and local DB paths")

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    cmd = args.command or "web"

    if cmd == "web":
        return run_web(open_browser=not getattr(args, "no_open", False))
    if cmd == "list":
        return cli_list_playlists()
    if cmd == "generate":
        return cli_generate(args.pid)
    if cmd == "apply":
        return cli_apply(args.pid)
    if cmd == "doctor":
        return cli_doctor()
    if cmd == "rescue":
        if args.list:
            print(json.dumps({"backups": list_backups()}, ensure_ascii=False, indent=2))
            return 0
        timestamps = _backup_timestamps()
        target = args.timestamp
        if args.latest:
            target = timestamps[-1] if timestamps else None
        if not target:
            print("[ERROR] specify --list or (--latest/--timestamp)")
            return 2
        if not args.yes:
            print("[ERROR] rescue restore requires --yes")
            return 2
        ok, msg = restore_backup(target, restart_music=not args.no_restart)
        if ok:
            print(f"[OK] {msg}")
            return 0
        print(f"[ERROR] {msg}")
        return 1

    print(f"[ERROR] unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
