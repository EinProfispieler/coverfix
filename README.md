# CoverFix

A local web UI (macOS) for fixing and managing Apple Music playlist artwork.

## Signature
![Evil Panda MD Production](assets/Epanda.png)

Evil Panda MD Production

## What It Does
- Lists normal user playlists from Apple Music.
- Generates a candidate cover from the first available track artwork in each playlist.
- Previews the current artwork stored by Apple Music and the generated candidate side-by-side.
- Applies selected playlist covers through the local Apple Music artwork database.
- Batches selected playlists so Music quits and reopens only once per apply run.

## Requirements
- macOS with the Apple Music app
- Python 3.9+
- Pillow (`pip3 install Pillow`)

## Run
```bash
chmod +x run.sh
./run.sh
```

The UI opens at: <http://127.0.0.1:8765>

Or directly:
```bash
python3 playlist_cover_helper_web.py
```

Legacy Tk version (optional):
```bash
python3 playlist_cover_helper.py
```

## First-Launch Permissions
You may be prompted for:
- **Apple Events** permission for controlling Music.
- **Accessibility** permission if you use the legacy Tk automation flow.

## Workflow
1. Click **Refresh**.
2. Select one or more playlists via the checkboxes.
3. Use **Generate Selected** to inspect candidates, or **Apply** to generate and apply in one batch.
4. Check **Current in Music** after Music restarts.

## CI Packaging (macOS Only)

GitHub Actions packages CoverFix on every push using a macOS runner.

- Workflow: `.github/workflows/package-macos.yml`
- Output: `CoverFix-macos-<short_sha>.zip`
- Location: GitHub Actions run artifacts

## Output Folder
Generated covers are written to:
```
~/.coverfix/covers
```

## CLI Usage

CoverFix also works headlessly without the web UI.

```bash
# List all playlists (shows pid, track_count, name)
python3 playlist_cover_helper_web.py list

# Generate covers for specific playlists (by pid)
python3 playlist_cover_helper_web.py generate --pid <pid>
python3 playlist_cover_helper_web.py generate --pid <pid1> --pid <pid2>

# Generate AND apply covers in one step
python3 playlist_cover_helper_web.py apply --pid <pid>
python3 playlist_cover_helper_web.py apply --pid <pid1> --pid <pid2>

# Explicitly start web UI
python3 playlist_cover_helper_web.py web
python3 playlist_cover_helper_web.py web --no-open  # skip auto-opening browser

# Diagnostics
python3 playlist_cover_helper_web.py doctor
```

Get the `pid` for a playlist by running `list` first — it appears in the first column.

### Rescue From CLI (No Web Button)

`rescue` is intentionally CLI-only because it is destructive.

```bash
# 1) Inspect backups first
python3 playlist_cover_helper_web.py rescue --list

# 2) Restore latest backup (destructive, requires --yes)
python3 playlist_cover_helper_web.py rescue --latest --yes

# 3) Restore a specific backup timestamp
python3 playlist_cover_helper_web.py rescue --timestamp 20260512-143000 --yes
```

What restore does:
- Stops Music and artwork background services
- Replaces `artworkd.sqlite` / `-wal` / `-shm` with backup files
- Starts Music again (unless `--no-restart` is provided)

Use restore only when library artwork is broken and you understand that recent artwork changes after the backup point may be lost.

## How It Works
CoverFix talks to Apple Music via AppleScript to enumerate playlists and trigger artwork refreshes, then writes images directly into the local `artworkd.sqlite` database used by `AMPArtworkAgent`. Music is restarted once per batch so changes take effect.

## Troubleshooting

If features suddenly stop working, run:

```bash
python3 playlist_cover_helper_web.py doctor
```

If `fetch_playlists_ok` is `false`, first reboot macOS, then open Music manually and retry.

## License
MIT — see [LICENSE](LICENSE).
