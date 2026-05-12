# Privacy Policy

CoverFix is designed for local macOS use with Apple Music / Music.app.

## Data handled by CoverFix

CoverFix may access Apple Music playlist information and playlist artwork data as needed for its playlist cover repair workflow.

Depending on the command or UI action, it may interact with:

- Apple Music / Music.app
- Local playlist metadata such as playlist names and persistent IDs
- Local track metadata used to find candidate artwork
- Local image files generated or selected during repair
- Local temporary and backup files used during repair
- The local Apple Music artwork database

## Music files

CoverFix is focused on Apple Music playlist covers.

It is not intended to upload, share, or modify the audio content of your music files. It is not a music tag editor.

## Network access

Most CoverFix operations are local.

When a playlist has no usable local track artwork, CoverFix may query Apple's public iTunes Search API to find candidate artwork. That fallback can send the first track's title, artist, album, and country search parameters to Apple, then download an artwork image from Apple's artwork CDN.

CoverFix should not upload your music files or full playlist library to a remote server.

If future versions add other online features, they should clearly explain:

- What data is sent
- Where it is sent
- Why it is needed
- How to disable it

## Permissions

macOS may ask for permissions such as access to Music.app, Automation permission, or local file access.

These permissions should only be used for the playlist cover repair workflow.

## Backups

Before running tools that modify Apple Music library-related data, keep a backup of your Music library.

When applying covers, CoverFix creates timestamped backups of the local Apple Music artwork database files before writing changes.
