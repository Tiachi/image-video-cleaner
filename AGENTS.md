# Image/video cleaning project rules

- This standalone project handles images and video visuals only. Do not add sound processing, source separation, transcription, or acoustic task labels without a new user request. Extra original fields remain untouched and are not media inputs.
- Preserve original annotations/media. Route original JSONL row bytes to stage outputs; derived labels go in audit/attribute sidecars.
- Every stage keeps accepted, rejected, review, audit, summary/report, identity and checksum receipts. Success means stage execution, not human-verified label accuracy or a formal training-format certificate.
- Stage02 consumes formal Stage01 accepted bytes. Stage03 consumes formal Stage02 accepted bytes. No implicit resurrection of rejected/review rows.
- Follow clip-aware cache identities, namespace/profile separation, immutable atomic publication, source/decoder metadata checks and explicit cache-only mode. Do not read UniMoE's RGB/JPEG tensor caches as if they were this project's PNG frames.
- Never traverse a large media tree. Read exact annotation-derived allowlisted paths, bound concurrency/decoding, and stop on cache corruption/capacity exhaustion.
- No datasets, media, cache, SQLite, logs, model weights, credentials, private runtime configs or private keys in Git. Stage exact reviewed paths; never blanket stage.
- Default private upstream: https://github.com/Tiachi/image-video-cleaner. Main is stable; use codex/ task branches for later changes. Verify owner/name, visibility and remote commit SHA. Preserve dirty checkouts and use fast-forward synchronization only.
- Direct server inspection and reviewed transfers are authorized for requested work. No personal proxy for downloads/Git unless explicitly authorized for that operation. Do not start a model or real data scan until inputs/config are specified.
- Keep runbooks and provenance in docs/. Record documented/implemented/locally tested/server-tested states separately.
