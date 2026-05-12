#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple


ARTWORK_DB = Path.home() / "Library/Containers/com.apple.AMPArtworkAgent/Data/Documents/artworkd.sqlite"
ARTWORK_DIR = Path.home() / "Library/Containers/com.apple.AMPArtworkAgent/Data/Documents/artwork"

FMT_JPEG = 0x4A504547  # JPEG
FMT_PNG = 0x504E4766  # PNGf
FMT_TIFF = 0x54494646  # TIFF
KIND_CUSTOM = 102


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def _stop_music_stack() -> None:
    subprocess.run(["osascript", "-e", 'tell application "Music" to quit'], check=False)
    subprocess.run(["pkill", "-f", "AMPArtworkAgent"], check=False)
    time.sleep(0.8)


def _start_music() -> None:
    subprocess.run(["open", "-a", "Music"], check=False)


def _detect_format_and_ext(img: Path) -> Tuple[int, str]:
    raw = img.read_bytes()
    if raw.startswith(b"\xFF\xD8\xFF"):
        return FMT_JPEG, "jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1A\n"):
        return FMT_PNG, "png"
    if raw.startswith(b"II*\x00") or raw.startswith(b"MM\x00*"):
        return FMT_TIFF, "tiff"

    ext = img.suffix.lower().lstrip(".")
    if ext in {"jpg", "jpeg"}:
        return FMT_JPEG, "jpeg"
    if ext == "png":
        return FMT_PNG, "png"
    if ext in {"tif", "tiff"}:
        return FMT_TIFF, "tiff"
    raise ValueError(f"Unsupported image format: {img}")


def _image_size(img: Path) -> Tuple[int, int]:
    p = _run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(img)])
    if p.returncode != 0:
        raise RuntimeError(f"sips failed: {p.stderr.strip() or p.stdout.strip()}")
    m_w = re.search(r"pixelWidth:\s*(\d+)", p.stdout)
    m_h = re.search(r"pixelHeight:\s*(\d+)", p.stdout)
    if not m_w or not m_h:
        raise RuntimeError(f"Could not parse size from sips output: {p.stdout}")
    return int(m_w.group(1)), int(m_h.group(1))


def _pid_hex_to_signed_int64(pid_hex: str) -> int:
    s = pid_hex.strip().upper()
    if not re.fullmatch(r"[0-9A-F]{16}", s):
        raise ValueError("PID must be exactly 16 hex chars, e.g. 45138FED3D7639BA")
    v = int(s, 16)
    if v >= 1 << 63:
        v -= 1 << 64
    return v


def _backup_db(db: Path) -> None:
    ts = time.strftime("%Y%m%d-%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db) + suffix)
        if src.exists():
            dst = Path(str(src) + f".bak.{ts}")
            shutil.copy2(src, dst)
            print(f"[backup] {src} -> {dst}")


def list_playlists() -> int:
    script = r'''
set oldDelims to AppleScript's text item delimiters
set AppleScript's text item delimiters to tab
tell application "Music"
    set ps to (every user playlist whose smart is false and special kind is none)
    set rows to {}
    repeat with p in ps
        set end of rows to ((persistent ID of p) & tab & (name of p))
    end repeat
    set AppleScript's text item delimiters to linefeed
    set outText to rows as text
end tell
set AppleScript's text item delimiters to oldDelims
return outText
'''
    p = _run(["osascript", "-e", script])
    if p.returncode != 0:
        print(p.stderr.strip() or p.stdout.strip(), file=sys.stderr)
        return 1
    print(p.stdout.strip())
    return 0


def inject_cover(pid_hex: str, image_path: Path, restart_music: bool) -> int:
    if not ARTWORK_DB.exists():
        print(f"artwork DB not found: {ARTWORK_DB}", file=sys.stderr)
        return 1
    if not ARTWORK_DIR.exists():
        print(f"artwork dir not found: {ARTWORK_DIR}", file=sys.stderr)
        return 1
    if not image_path.exists():
        print(f"image not found: {image_path}", file=sys.stderr)
        return 1

    pid_int = _pid_hex_to_signed_int64(pid_hex)
    fmt_code, ext = _detect_format_and_ext(image_path)
    w, h = _image_size(image_path)
    sha = hashlib.sha256(image_path.read_bytes()).hexdigest().upper()
    dst_artwork = ARTWORK_DIR / f"{sha}_sk_{KIND_CUSTOM}_cid_1.{ext}"

    _stop_music_stack()
    _backup_db(ARTWORK_DB)

    con = sqlite3.connect(str(ARTWORK_DB))
    try:
        cur = con.cursor()
        row = cur.execute(
            "select ZSOURCEINFO from ZDATABASEITEMINFO where ZPERSISTENTID=?",
            (pid_int,),
        ).fetchone()
        if not row:
            print(f"playlist PID not found in artwork DB: {pid_hex} ({pid_int})", file=sys.stderr)
            return 2
        source_pk = int(row[0])

        srow = cur.execute(
            "select ZIMAGEINFO from ZSOURCEINFO where Z_PK=?",
            (source_pk,),
        ).fetchone()
        if not srow or srow[0] is None:
            print(f"source row has no image info (source={source_pk}); unsupported state", file=sys.stderr)
            return 3
        imageinfo_pk = int(srow[0])

        crow = cur.execute(
            "select Z_PK from ZCACHEITEM where ZIMAGEINFO=?",
            (imageinfo_pk,),
        ).fetchone()
        if not crow:
            print(
                f"cache row missing for image info {imageinfo_pk}; refusing to create coredata rows automatically",
                file=sys.stderr,
            )
            return 4

        shutil.copy2(image_path, dst_artwork)

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
    finally:
        con.close()

    print(f"[ok] injected cover for PID={pid_hex}")
    print(f"[ok] source image file: {dst_artwork}")
    if restart_music:
        _start_music()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Inject custom cover into Apple Music playlist artwork DB.")
    ap.add_argument("--list", action="store_true", help="List user playlists (PID + name)")
    ap.add_argument("--pid", help="Playlist persistent ID (16-hex, e.g. 45138FED3D7639BA)")
    ap.add_argument("--image", help="Path to image file (jpg/png/tiff)")
    ap.add_argument("--no-restart", action="store_true", help="Do not re-open Music after injection")
    args = ap.parse_args()

    if args.list:
        return list_playlists()

    if not args.pid or not args.image:
        ap.error("need --pid and --image (or use --list)")

    return inject_cover(args.pid, Path(args.image).expanduser(), restart_music=not args.no_restart)


if __name__ == "__main__":
    raise SystemExit(main())
