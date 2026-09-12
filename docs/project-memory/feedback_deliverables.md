---
name: feedback-deliverables
description: How the user wants code and files delivered — written directly into the StartupAgents folder, never as archives/tarballs handed over in chat.
type: feedback
---

Write everything directly into `/Users/kamal/StartupAgents/` (the code under `StartupAgents/startupos/`). Never hand the user a tar/zip to unpack.

Why: the user said so on 2026-09-04 ("I want you to write everything in this folder. Not give me a tar file etc").
How to apply: build in the container if needed, but the delivery step is always device_bash/device_commit_files writing the actual files into the folder (many small files is fine). If a transfer archive is ever used internally, extract it in the folder and remove/move the archive so the user never sees it.
