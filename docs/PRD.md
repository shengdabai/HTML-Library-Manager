# HTML Library Manager

## User and outcome
One owner retrieves personal local HTML reports using short topic labels and full-text search, then opens original files immediately. In loopback service mode the owner can also edit a page in place and hand annotated changes to the local Codex CLI. User recruitment/payment N/A (personal use). Scanning and search never modify sources; only an explicit save or an applied Codex job writes, always after a version backup.

## Must (≤7)
1. Discover HTML/HTM within explicit source roots, excluding hidden, credential, dependency, cache and symlink paths. Show coverage, skipped/failed files, and bounded scan status. Integration.
2. Extract title/headings/body without executing scripts; topic label up to 20 characters, preserve original filename, give evidence snippet and derivation. Unit + real sample.
3. Search filename/title/label/body; folder filter and modified/name sorting. Browser.
4. Click open original HTML through an indexed identifier, preserving original relative assets; stale source gives actionable error. Integration/browser.
5. Refresh additions/changes/removals without editing sources; persistent favorites/recent history with clean empty/error states. Integration/browser.
6. Responsive, keyboard usable UI at 390/768/1440; no untrusted markup injection or external UI assets. Browser.
7. Restartable local entry, instructions and scoped security checks appropriate to selected runtime. Real dependency.

## Editing (2026-10-11, service mode only)
E1. Edit text and inline formatting in place; element styles, move/duplicate/delete/hide, HTML fragment edit; image alt and local image replacement into `<stem>_assets/`. Browser.
E2. Save with version backup, atomic write, base-digest conflict refusal (409) and single-file reindex; version list and restore. Unit + browser.
E3. Annotations stored outside the page (text/layout/image/other; open/sent/done). Unit + browser.
E4. Codex jobs: isolated copy, `codex exec` workspace-write, logs/elapsed/cancel, apply only if the original is unchanged, backup before write-back, copy new assets without overwriting, one-click undo; restart marks running jobs interrupted. Unit (fake codex) + real smoke.
E5. Page scripts never run in the editor (sandboxed iframe without allow-scripts + `script-src 'none'` CSP); editor markup never saved. Browser.

## Should
Duplicate hints (never automatic deletion), source configuration, result counts and pagination, accessible status updates and shortcut / to search.

## Won't
Source delete/rename/move, cloud upload, remote AI summarization, login/payment, executing page scripts in the manager (the editor preview is script-disabled), unattended login startup, automatic volume mounting, unbounded whole-disk crawl.

## Pending
Dictation has contradictory deployment wording; no persistent server/startup installation until user resolves. Shared metadata index and UI draft can proceed.

## Review
ChatGPT: sanitized product/UI consultation; recommendations adopted only after local checks. Claude: read-only CC review. NLPM: inspect actual installed rubric; plugin-only checks are N/A for an app, do not fabricate plugin manifests or scores.
