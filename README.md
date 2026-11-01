# FRIDAY

FRIDAY is a local, Windows-only desktop assistant that takes typed or spoken
requests and carries them out on your computer through a registry of "skills"
(files, apps, windows, browser, shell, system settings, memory, scheduling and
more). A small local language model plans multi-step goals. Every action passes
through a permission tier, is written to an audit log, and, where a read-back
check exists, is verified against the real state of the machine rather than
trusted from the model's say-so.

It is a personal project developed in numbered phases (about 100 so far). The
design goal is an agent that runs locally, with no cloud service required for
its core loop, and that is honest about what it did and did not manage to do.

## Project maturity

**Experimental. Not production-ready, and not claimed to be safe.**

- No clean-machine installation has been tested.
- There is no recorded human validation pass of the GUI or voice features
  (`MANUAL_VALIDATION.md` is an unticked checklist that stops at Phase 36).
- The agent benchmark was last run around Phase 32; its scores are stale and are
  not quoted here.
- Several known defects remain open, including cases where STOP does not
  interrupt running work. See [Known limitations](#known-limitations).
- Test counts quoted in internal notes have not been re-verified.

Run it only on a machine and with data you can afford to experiment on.

## Current capabilities

Implemented in source (not all validated end to end):

- **Entry points:** desktop GUI with tray icon, text console, one-shot `say`,
  and a local HTTP/WebSocket daemon.
- **Skills:** about 107 registered skills by a static count of the decorators
  (not a runtime count), spread over four permission tiers.
- **Intent matching:** local embedding match first; a language-model planner for
  multi-step goals (`plan.run`).
- **Permissions:** four tiers (L0 read, L1 reversible write, L2 destructive,
  L3 external). L0 and L1 run automatically, L2 and L3 ask for confirmation.
  Scheduled and trigger jobs are capped at L1.
- **Audit and outcomes:** an audit row is written before a skill runs and closed
  afterwards with an outcome (success, failure, not started, cancelled, timeout,
  unknown outcome). Interrupted work is reconciled at the next start.
- **Verification:** about 32 skills have a read-back check; about 35 (shell, UI
  automation, browser and WhatsApp skills among them) are unverifiable.
- **File recovery:** deletes and overwrites keep a snapshot that can be restored
  through operator-only recovery commands.
- **Voice:** wake word, voice-activity detection, speech to text and speech
  output, plus push-to-talk (GUI only).
- **Shell commands** run inside a Windows job object so a stopped command's
  process tree can be ended.

## Key features

- Local-first: language model, embeddings and speech run on your machine.
- Credentials are scrubbed from audit views, logs and several output paths.
  This is pattern-based and **is not a guarantee that every output is
  credential-safe**.
- Outcomes distinguish "failed" from "unknown", and restarts do not turn
  unknown into success.
- Reversible file operations through a snapshot store.

## Technology stack

Python 3.12 · Ollama (`qwen2.5:3b` by default) · PySide6 GUI · FastAPI/uvicorn
daemon · SQLite (WAL) · fastembed (`BAAI/bge-small-en-v1.5`) · faster-whisper
`base` · openWakeWord (default phrase `hey_jarvis`) · Silero VAD · pyttsx3/SAPI ·
Playwright · optional Tesseract OCR · Windows DPAPI key store.

## Requirements

Windows, Python 3.12, a local Ollama install with the configured model, and
network access on first run for model downloads. Other platforms and Python
versions are unsupported or untested.

## Quick installation reference

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama pull qwen2.5:3b
START.bat
```

This is **untested on a clean machine**, and `requirements.txt` has a suspected
Pillow/fastembed version conflict. Read the
[installation guide](docs/getting-started/installation.md) first, including the
fresh-clone wake-word limitation.

## Documentation

- [Installation](docs/getting-started/installation.md)
- [Usage](docs/getting-started/usage.md)
- [Configuration](docs/getting-started/configuration.md)

`PLAN.md` is the original development log. It stops at Phase 36 in detail, and
parts of it are out of date.

## Known limitations

These are drawn from reading the source and from internal audit notes. They have
not all been reproduced at runtime.

- **STOP does not interrupt everything.** It acts only while a multi-step goal
  (`plan.run`) is running or a confirmation is pending. A directly requested
  `shell.run`, `routine.run`, `schedule.run_now`, scheduled jobs and trigger jobs
  are not interrupted when no goal is active, and STOP answers "nothing running
  to stop". See [usage](docs/getting-started/usage.md).
- **Credential redaction is incomplete.** It is pattern-based and cannot catch a
  bare secret with no keyword, and some paths put raw exception text into
  planner steps or notifications. The redaction patterns also degrade
  quadratically on long, separator-dense text.
- **Wake word:** the phrase is `hey_jarvis`, not "FRIDAY", and the personal
  verifier model is absent on a fresh clone. The author-reported accuracy of the
  verifier on a 12-clip set was 9/12.
- **The local daemon has no authentication** on loopback. Remote mode requires a
  token but has no TLS.
- **Unverified areas:** WhatsApp (mock page only), multi-monitor and DPI
  behavior of UI automation, behavior on power loss, and a rare native crash at
  process exit that was investigated but not resolved.
- Recovery snapshots are stored unencrypted.

## License

No license file is present yet. Until the maintainer adds one, no license is
granted.
