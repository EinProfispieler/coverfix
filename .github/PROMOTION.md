# GitHub Promotion Checklist

Use this file when preparing the repository page, a first tagged release, or a social share.

## Repository description

```text
A local Python tool to fix missing Apple Music playlist covers on macOS.
```

## Repository topics

Recommended GitHub topics:

- `apple-music`
- `macos`
- `music-app`
- `playlist`
- `playlist-cover`
- `cover-art`
- `python`
- `applescript`
- `itunes`
- `music-library`

Use `applescript` because CoverFix uses `osascript` to talk to Music.app.

## Social preview

GitHub social preview images are configured manually in repository settings.

Suggested source:

- Use a 1280 x 640 PNG or JPG.
- Show the CoverFix name clearly.
- Include the one-line positioning: `Fix missing Apple Music playlist covers on macOS`.
- Use the existing web UI screenshot as the product proof.
- Avoid phrases such as `app download`, `installer`, `build from source`, `Xcode`, or `Mac App Store`.

Suggested social preview text:

```text
CoverFix
Fix missing Apple Music playlist covers on macOS
Local Python tool. Preview first, apply with backups.
```

## Release notes

Use [RELEASE_NOTES.md](../RELEASE_NOTES.md) as the source for the first tagged release notes.

Keep release wording clear that CoverFix is a Python source project. Do not imply a signed app, installer, Xcode project, or Mac App Store release unless those actually exist.

## README review

Before promoting:

- Confirm the entry command still works: `python3 playlist_cover_helper_web.py`.
- Confirm the screenshot is current.
- Confirm privacy notes still match network behavior.
- Confirm no personal paths, tokens, credentials, private playlist data, or generated `.pyc` files are committed.
