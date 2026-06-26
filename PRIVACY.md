# Privacy

**Code is public — data is private.**

This project ships an engine (code), not data. The engine does not collect,
upload, or transmit your recordings, transcripts, voices, or names anywhere.
Everything stays on your machines and in **your own** storage (the "hub" you
configure).

## Never committed to this repository

- **Secrets:** API tokens, OAuth credentials, `*.local.json`, `*.env`
- **Voiceprints — biometric personal data:** `_voiceprints/`, `**/Speakers/`,
  `*voiceprint-profiles.json` and any local voice cache
- **User content:** audio, video, transcripts, speaker names, personal paths

These are enforced by `.gitignore`. The repository contains only code, schemas,
`*.example` templates, and documentation with placeholder values.

## Voiceprints and consent

A voiceprint is a numeric representation of a person's voice and is treated as
**biometric personal data**. If you enroll voices, you are responsible for having
a lawful basis and the appropriate consent of the people involved, and for
storing that data securely in your own private hub.

## Your responsibilities as an operator

- Keep tokens in per-machine credential stores or environment variables.
- Keep voiceprint registries and transcripts in private storage, never in a
  public location.
- Review `.gitignore` before committing if you fork or extend this project.
