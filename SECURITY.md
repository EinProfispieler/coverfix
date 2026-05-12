# Security Policy

## Supported versions

CoverFix is currently an early-stage project.

Security fixes should target the latest version on the `main` branch unless a release policy is added later.

## Reporting a vulnerability

If you find a security issue, open a GitHub issue with a clear description.

Do not include sensitive personal data, private Apple Music library data, API keys, tokens, credentials, or local file paths in public issue reports.

## Security expectations

CoverFix should avoid:

- Uploading user music files
- Logging sensitive local paths unnecessarily
- Committing test data from a real personal music library
- Storing tokens, credentials, or private local configuration in the repository
- Adding online features without documenting what data is sent and why
