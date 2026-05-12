# Release Notes

## Draft v0.1.0

Initial public version of CoverFix.

CoverFix is a local Python tool for fixing missing, blank, or broken Apple Music playlist covers on macOS.

Current focus:

- Apple Music / Music.app playlist artwork
- Local macOS workflow
- Web UI for previewing current and generated playlist covers
- CLI commands for listing playlists, generating cover candidates, applying covers, diagnostics, and backup restore
- Timestamped artwork database backups before apply operations

What this is not:

- Not an album artwork metadata editor
- Not an MP3 / M4A / FLAC tag editor
- Not a MusicBrainz Picard replacement
- Not an installable macOS app
- Not a Mac App Store release

Known limitations:

- Apple Music / iCloud sync may be delayed.
- Some playlist artwork issues may be caused by Apple-side caching.
- Behavior may vary across macOS versions.
- Applying covers modifies Apple Music's local artwork database, so users should keep a Music library backup.

Suggested release title:

```text
CoverFix v0.1.0
```

Suggested short description:

```text
Initial public version of CoverFix, a local Python tool for fixing missing Apple Music playlist covers on macOS.
```
