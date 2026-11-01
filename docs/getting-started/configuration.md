# Configuration

This page describes how FRIDAY is configured: the `config.yaml` file, the environment variables it reads, where it keeps its data, the daemon's network settings, and which `config.yaml` keys have an effect. Claims were checked against the source files named in each section. Anything not checked is marked **unverified**.

## 1. How `config.yaml` is loaded

Source: `friday/config.py`, `friday/paths.py`.

- The file is `config.yaml` in the repository root (`paths.CONFIG_PATH = ROOT / "config.yaml"`).
- It is read **once**, when `friday.config` is first imported: `CFG = load()` at module level. Code uses it through `from friday.config import CFG`. Editing the file has no effect on a running process; restart FRIDAY. (A few call sites read `CFG` at call time, but the file itself is not re-read.)
- `load()` does `yaml.safe_load(...)`. If the file does not exist or is empty, every value comes from defaults.
- Defaults are defined in code, as pydantic models in `friday/config.py`. Any key missing from `config.yaml` falls back to its code default. Several keys that have code defaults are not listed in the shipped `config.yaml` at all (see section 5).
- **Unknown keys are silently ignored.** The models do not set `extra="forbid"`, and this was not tested by running code (unverified at runtime), but nothing in `config.py` rejects extra keys. A misspelled key therefore does nothing and produces no error.
- **Type errors are not ignored.** A value of the wrong type (for example `port: "abc"`) fails pydantic validation when `friday.config` is imported.
- Some fields are intentionally plain `str`, not enums, so a bad value fails later with a runtime message instead of at load (`llm.provider`, `voice.tts.engine`, `voice.wakeword.engine`).

### Sections that exist only in code

- `storage` (`storage.db_path`) is defined in `friday/config.py` (`StorageConfig`) but is **not present in the shipped `config.yaml`**. Add it yourself if you need it:

  ```yaml
  storage:
    db_path: ""   # empty = %LOCALAPPDATA%\FRIDAY\friday.db
  ```

- Individual code-only keys inside sections that do appear in `config.yaml`: `brain.recovery_routing`, `planner.max_discovery_steps`, `planner.max_discovery_time_s`, `planner.max_evidence_items`, `planner.decision_repair_attempts`, `planner.structured_output`, `planner.subgoal_evidence_advance`, `planner.answer_from_evidence`, `planner.answer_grounding_guard`, `intelligence.context_memory_enabled`, `intelligence.context_max_items`, `intelligence.context_confidence_threshold`, `intelligence.context_consequential_confidence_threshold`, `intelligence.personalization_confidence_threshold`, `intelligence.context_block_max_chars`, `voice.wakeword.window_s`, `voice.wakeword.persist_frames`, `voice.wakeword.debug`.

## 2. Environment variables

Every `os.environ` / `os.getenv` read under `friday/` was searched for. FRIDAY-specific variables:

| Variable | Read in | Semantics |
|---|---|---|
| `FRIDAY_DB` | `friday/paths.py` | Overrides the default database path. Value is stripped; empty or whitespace-only means "not set". `%VARS%` are expanded and `~` is expanded. Read **once at import** of `friday.paths`. `storage.db_path` in `config.yaml` takes precedence over it (`dbplace.resolve()`), unless a test has redirected `paths.DB_PATH`. |
| `FRIDAY_SNAPSHOTS` | `friday/paths.py` | Overrides the default recovery-store (snapshot) folder. Same stripping/expansion rules, read once at import. `files.snapshot_dir` in `config.yaml` takes precedence (`recovery.root()`). |
| `FRIDAY_KEY_FILE` | `friday/paths.py` | Overrides the location of the DPAPI-protected master key file. Same stripping/expansion rules, read once at import. There is no `config.yaml` key for it. |
| `FRIDAY_DAEMON_TOKEN` | `friday/daemon.py` (`TOKEN_ENV`), `friday/log.py` | Shared secret for remote mode. See section 4. `friday/log.py` also replaces any occurrence of this value in log text with `[redacted]`. Never read from `config.yaml`. |
| `FRIDAY_REFUSE_REAL_DB` | `friday/dbplace.py` (`is_test_entrypoint`) | Test/safety tripwire control. When it is exactly `"1"`, the process is treated as a test or benchmark entry point, so opening the real database, real recovery store or real key file raises `IsolationError`. |
| `FRIDAY_ALLOW_REAL_DB` | `friday/dbplace.py` (`is_test_entrypoint`) | Test/safety tripwire control. When it is exactly `"1"`, it turns **off** the automatic test-entry-point detection. It exists for maintainers who deliberately need a test run to touch real data; it is not a normal setting, and you should not set it. It is checked before `FRIDAY_REFUSE_REAL_DB`, so it wins if both are set. |

### How the tripwire works

`dbplace.is_test_entrypoint()` returns true automatically (no variable needed) when `pytest` is loaded, or when the main script's file name matches `^(smoke_|test_|bench|agent_|regression|phase\d+_)` (case-insensitive). In that case `dbplace.guard`, `guard_store` and `guard_key` refuse to use the real database, recovery store or key file. The error messages tell the caller to isolate the run (for example with `store.use_temp_db()`). This is a fail-fast safeguard for development runs, not a security boundary.

### Other variables read (system, not FRIDAY-specific)

| Variable | Read in | Use |
|---|---|---|
| `LOCALAPPDATA` | `paths.py`, `dbplace.py`, `dbscrub.py` | Base for `%LOCALAPPDATA%\FRIDAY\...` defaults. If it is empty or unset, the database and snapshots fall back to repo-relative `data/`; the key file becomes unavailable (`None`). |
| `OneDrive`, `OneDriveConsumer`, `OneDriveCommercial` | `dbplace.py`, `recovery.py`, `keystore.py` | Detect whether a path is inside a OneDrive folder (warnings, key-location safety check). |
| `USERPROFILE`, `APPDATA` | `keystore.py` | Enumerating cloud-sync folders (Dropbox `info.json`, folders directly under the profile) when deciding whether a key location is safe. |
| `APPDATA`, `PROGRAMDATA` | `skills/apps.py` | Locating Start Menu shortcuts for app launching. |

## 3. Where data lives

`%LOCALAPPDATA%\FRIDAY\` means the per-user local application-data folder. `data/` means the `data` folder inside the repository (`paths.DATA = ROOT / "data"`).

| What | Default location | Override | Notes |
|---|---|---|---|
| Runtime database `friday.db` (audit log, goals, episodes, corrections, memory, undo journal) | `%LOCALAPPDATA%\FRIDAY\friday.db` | `FRIDAY_DB` env var, or `storage.db_path` (wins over the env var; a relative path is relative to the repository) | Falls back to `data/friday.db` only if `LOCALAPPDATA` is unset. The old location `data/friday.db` is the "legacy" location; on first open of the default location, if nothing is there and the process is not a test entry point, the legacy database is copied over (SQLite online backup, validated, never overwriting). The legacy file is left in place and not used afterwards; FRIDAY logs a warning telling you to review and delete it yourself. |
| Recovery store (snapshots of files that `files.write`/`copy`/`move`/`delete` overwrite or remove) | `%LOCALAPPDATA%\FRIDAY\snapshots` | `FRIDAY_SNAPSHOTS` env var, or `files.snapshot_dir` (wins; relative path is relative to the repository) | Falls back to `data/snapshots` only if `LOCALAPPDATA` is unset. Old Phase 32 snapshots in `data/snapshots` are still read so old undo entries keep working. A drive root or the home folder is refused as a store. **Snapshots are unencrypted copies of your files.** |
| Master key file `fingerprint.key` | `%LOCALAPPDATA%\FRIDAY\fingerprint.key` | `FRIDAY_KEY_FILE` env var | Holds only a Windows DPAPI-protected blob (current-user scope). Never created under the repository, `data/`, or a detected cloud-synced folder; if no safe location exists the key store raises `KeyUnavailable` (no plaintext fallback). Used by `friday/objectivefp.py` (the only module besides `paths`/`keystore` that references the key store). |
| Database scrub backups | `%LOCALAPPDATA%\FRIDAY-backups\` (falls back to a `FRIDAY-backups` folder under the user's home directory if `LOCALAPPDATA` is unset) | `--backup-dir` option of the scrub command | Must be outside the repository, outside cloud-synced folders and outside the database's own folder (`dbscrub.check_backup_root`). A backup keeps the data that the scrub removed from the live database, and FRIDAY does not delete it. |
| Logs | `data/logs/friday.log` | none (`logging.level` only changes verbosity) | Rotating file, 5,000,000 bytes per file, 3 backups. Lines pass through a credential-redaction filter (`RedactingFilter`); this is pattern-based and best-effort, so do not treat logs as guaranteed free of sensitive text. |
| Browser profile | `data/browser_profile` (`browser.profile_dir`, resolved as `ROOT / profile_dir`) | `browser.profile_dir` (an absolute path replaces the repository-relative one) | Persistent profile for FRIDAY's own controlled browser (logins survive restarts). It is separate from your normal Chrome profile. |
| Models and cache | `data/models`, `data/cache` | none | `paths.ensure()` creates `data/`, `data/logs`, `data/models`, `data/cache`. The wake-word model folder defaults to `data/models/openwakeword` (`voice.wakeword.models_dir`). Screenshots, if enabled, go to `data/cache`. |

### Cloud sync

`data/` is inside the repository and is git-ignored (`.gitignore` contains `data/`). If you keep the repository in a cloud-synced folder (OneDrive, Dropbox, Google Drive, iCloud), then **everything under `data/` may be uploaded**: logs, the browser profile, models, cache, and the legacy database/snapshots if they exist. The default database, snapshot, and key locations were moved to `%LOCALAPPDATA%\FRIDAY\` for this reason, but only because `LOCALAPPDATA` is normally not synced; whether that holds on your machine is up to your sync configuration (unverified for any given setup). FRIDAY logs a warning when it detects that the database is inside a synced folder (name-based, best-effort detection).

## 4. Daemon

Source: `friday/daemon.py`, `config.yaml`.

```yaml
daemon:
  host: 127.0.0.1
  port: 8765
  allow_remote: false
```

- Defaults are `127.0.0.1` and `8765` (code and `config.yaml` agree). `serve()` runs uvicorn on `CFG.daemon.host` / `CFG.daemon.port`.
- **Local mode (default) has no authentication.** Any program running on the same computer can call the daemon.
- **Bind check:** at startup `check_bind()` refuses to start if `host` is not a loopback address (`127.0.0.1`, `::1`, `localhost`, or any address `ipaddress` reports as loopback) while `allow_remote` is false.
- **Host/Origin guard** (`LocalOnlyMiddleware`, applies to HTTP and WebSocket):
  - With `allow_remote` false, a request whose `Host` header is not a loopback name is refused with HTTP 403 (this is the DNS-rebinding defence).
  - A request that carries an `Origin` header is refused unless the origin's host:port equals the `Host` header. Clients that send no `Origin` (command-line tools, local programs) pass this check.
  - With `allow_remote` true, the non-loopback `Host` check is skipped, but the same-origin rule for requests with an `Origin` still applies.
  - A refused WebSocket is closed without being accepted (code 1008).
- **Remote mode** (`allow_remote: true`):
  - Requires the environment variable `FRIDAY_DAEMON_TOKEN`. The daemon refuses to start in remote mode without a usable token.
  - A token is "usable" only if, after stripping whitespace, it is **at least 32 characters** (`MIN_TOKEN_CHARS = 32`) **and contains at least 8 distinct characters** (`len(set(token)) >= 8`). Otherwise it is treated as missing.
  - Every HTTP request and every WebSocket handshake must send `Authorization: Bearer <token>`. The scheme name is matched case-insensitively and the token is compared with `hmac.compare_digest`. Failures return HTTP 401 with `WWW-Authenticate: Bearer` (WebSocket: closed 1008), and the reply is the same whether the header is missing, malformed or wrong.
  - The token is a shared secret, not a user identity: it does not say who is calling and gives no per-user rights.
  - The token is read from the environment, never from `config.yaml`; it is not generated by FRIDAY.
- **No TLS.** The daemon speaks plain HTTP. In remote mode the bearer token travels unencrypted unless you add a VPN or SSH tunnel (the comments in `config.yaml` recommend this).
- The `/ws` event stream is described in `config.yaml` as status-only (which skill ran, how long, whether it worked). The topic allow-list is in `friday/remote_events.py`, which was not read for this page (unverified).

## 5. Config keys

### 5a. Keys verified as read by code

"Default" is the code default in `friday/config.py` (values in the shipped `config.yaml` are the same unless noted). "Source" is a file where the key is read; it is not necessarily the only one.

#### daemon

| Key | Default | Effect | Source |
|---|---|---|---|
| `host` | `127.0.0.1` | Bind address; non-loopback refused unless `allow_remote` | `daemon.py`, `cli.py` |
| `port` | `8765` | Bind port; also used by the CLI to find the daemon | `daemon.py`, `cli.py` |
| `allow_remote` | `false` | Enables remote mode (requires token) and relaxes the Host check | `daemon.py` |

#### identity

| Key | Default | Effect | Source |
|---|---|---|---|
| `name` | `FRIDAY` | Display name in the CLI, tray, window title and notifications | `cli.py`, `notify.py`, `gui/main_window.py`, `gui/tray.py`, `intelligence/proactive.py` |
| `wake_word` | `hey friday` | Typed-text normalisation only: if the lower-cased request **starts with** this phrase, the phrase is stripped before intent matching. It does **not** configure the audio wake word (see 5b) | `brain/normalize.py` |

#### brain

| Key | Default | Effect | Source |
|---|---|---|---|
| `match_threshold` | `0.62` | Similarity needed to accept an intent match | `brain/engine.py` |
| `clarify_threshold` | `0.45` | Below this FRIDAY asks for clarification | `brain/engine.py` |
| `embedding_model` | `BAAI/bge-small-en-v1.5` | Embedding model for intent matching | `brain/matcher.py` |
| `direct_route_guard` | `true` | Single-skill routes whose arguments don't account for the request go to the planner | `directroute.py`, `session.py` |
| `recovery_routing` | `true` (code-only) | Exact sentences route to the recovery skills | `recoveryroute.py`, `session.py` |

#### permissions

| Key | Default | Effect | Source |
|---|---|---|---|
| `tiers` | L0 auto, L1 auto, L2 confirm, L3 confirm | Per-tier policy (`auto`/`confirm`/`deny`); unknown tier falls back to `confirm` | `permissions.py` |
| `unattended_ceiling` | `L1` | Highest tier a scheduled/triggered job may run | `permissions.py` |
| `overrides` | `{}` | Per-skill policy overriding the tier default | `permissions.py` |
| `stop_hotkey` | `ctrl+alt+x` | Global stop hotkey; blank disables | `gui/app.py` |

#### files

| Key | Default | Effect | Source |
|---|---|---|---|
| `max_snapshot_mb` | `256` | Larger files are not overwritten/deleted by the file skills (no recoverable copy means no change) | `recovery.py` |
| `snapshot_dir` | `""` | Recovery-store location override | `recovery.py`, `dbplace.py` |
| `snapshot_retention_days` | `30` | Minimum age before a cleanup may delete a copy (never less than 1 day) | `retention.py` |
| `snapshot_keep_newest` | `20` | Newest unreferenced copies a cleanup keeps | `retention.py` |
| `undo_window_days` | `0` | `0` = pending undo entries never expire; `N > 0` lets a cleanup retire older pending entries | `retention.py` |
| `snapshot_cleanup_batch` | `100` | Most copies deleted per cleanup run (minimum 1) | `retention.py` |

Cleanup of recovery copies is never automatic; it runs only when explicitly requested (per the `retention.py` docstring).

#### storage (code-only section)

| Key | Default | Effect | Source |
|---|---|---|---|
| `db_path` | `""` | Database location override | `dbplace.py` |

#### knowledge

| Key | Default | Effect | Source |
|---|---|---|---|
| `match_threshold` | `0.4` | Similarity for a chunk to count as relevant | `skills/knowledge.py` |
| `chunk_size` | `1000` | Characters per chunk | `knowledge.py` |
| `chunk_overlap` | `150` | Chunk overlap | `knowledge.py` |
| `max_files` | `500` | Cap when indexing a folder | `knowledge.py` |

#### ocr

| Key | Default | Effect | Source |
|---|---|---|---|
| `tesseract_cmd` | `""` | Path to `tesseract.exe`; blank autodetects | `ocr.py` |
| `min_confidence` | `0` | OCR words below this confidence are dropped | `ocr.py` |

#### browser

| Key | Default | Effect | Source |
|---|---|---|---|
| `channel` | `chrome` | Use installed Chrome via Playwright; blank = bundled Chromium; falls back automatically | `browser.py` |
| `headless` | `false` | Run without a visible window | `browser.py` |
| `profile_dir` | `data/browser_profile` | Persistent profile folder | `browser.py` |
| `nav_timeout_ms` | `20000` | Navigation timeout | `browser.py` |
| `action_timeout_ms` | `10000` | Action timeout | `browser.py` |

#### desktop_observer

| Key | Default | Effect | Source |
|---|---|---|---|
| `enabled` | `true` | Master switch for `screen.observe` and the planner's ambient context | `skills/screen.py`, `skills/plan.py` |
| `include_screenshot` | `false` | Save a screenshot with an observation | `desktop_observer.py` |
| `include_ocr` | `true` | Include OCR text (needs Tesseract) | `desktop_observer.py` |
| `max_ocr_chars` | `1000` | Cap on OCR text per observation | `desktop_observer.py` |
| `max_windows` | `20` | Cap on listed windows | `desktop_observer.py` |

#### llm

| Key | Default | Effect | Source |
|---|---|---|---|
| `provider` | `ollama` | Provider name (only Ollama is implemented) | `llm.py` |
| `base_url` | `http://localhost:11434` | Ollama server URL | `llm.py` |
| `model` | code `""`; shipped `config.yaml` sets `qwen2.5:3b` | Default model | `llm.py`, `skills/plan.py` |
| `temperature` | `0.3` | Sampling temperature | `llm.py` |
| `timeout_s` | `60.0` | Request timeout | `llm.py` |
| `num_ctx` | `6144` | Context window requested on every call; `0` sends nothing | `llm.py`, `orchestrator.py` |

#### planner

All in `config.py` `PlannerConfig`. Read mainly in `orchestrator.py`, `skills/plan.py`, `session.py`, `decision.py`, `intelligence/discovery.py`.

| Key | Default | Effect |
|---|---|---|
| `enabled` | `true` | Master switch for `plan.run` |
| `max_steps` | `16` | Step ceiling per run |
| `step_timeout_s` | `90.0` | Per-step timeout |
| `total_timeout_s` | `300.0` | Whole-run timeout (also used by the GUI backend) |
| `model` | `""` | Blank uses `llm.model` |
| `max_replans` | `2` | Forgiven failed steps per goal |
| `max_subgoals` | `8` | Cap on subgoal decomposition |
| `max_discovery_steps` | `6` (code-only) | Read-only discovery pre-phase step cap |
| `max_discovery_time_s` | `90.0` (code-only) | Discovery time cap |
| `max_evidence_items` | `8` (code-only) | Evidence cap |
| `decision_repair_attempts` | `1` (code-only) | Repair attempts for an invalid planner decision |
| `structured_output` | `true` (code-only) | Constrain planner replies to the decision JSON schema |
| `intent_guard` | `true` | Intent-to-action alignment check |
| `intent_prefilter` | `true` | Hide out-of-scope tools from the planner |
| `max_intent_rejections` | `3` | Rejections before a run stops |
| `goal_coverage` | `true` | Multi-part goals need evidence per part |
| `max_coverage_nudges` | `1` | Premature-done send-backs |
| `continuation_reads` | `true` | "read more" advances a page cursor |
| `scope_expansion` | `true` | Explicit follow-up may widen the previous read-only goal |
| `prompt_budget` | `true` | Shrink planner prompt to fit `llm.num_ctx` |
| `prompt_budget_fraction` | `0.85` | Fraction of the window the prompt may use |
| `chars_per_token` | `3.5` | Prompt-size estimate |
| `postcondition_verify` | `true` | Read state back after state-changing tools |
| `verify_timeout_s` | `4.0` | Bound for one read-back |
| `tool_data_excerpts` | `true` | Planner sees bounded tool-data excerpts |
| `tool_data_step_chars` | `500` | Per-step excerpt cap |
| `tool_data_total_chars` | `1500` | Whole-prompt excerpt cap |
| `semantic_repeat_guard` | `true` | Repeat guard compares what a call does |
| `confirmation_guard` | `true` | Bare "yes" with nothing pending asks what you mean |
| `subgoal_evidence_advance` | `true` (code-only) | Subgoal pointer advances on tagged evidence |
| `answer_from_evidence` | `true` (code-only) | Compose answer from gathered evidence |
| `answer_grounding_guard` | `true` (code-only) | Check final answer text against evidence |
| `tool_family_guard` | `true` | Browser vs desktop tool discipline |
| `max_family_rejections` | `3` | Rejections before a run stops |
| `action_completion` | `true` | "done" after only reading an action request is sent back |
| `max_action_nudges` | `1` | Send-backs |
| `counted_repeats` | `true` | "click it twice" runs exactly twice |

#### intelligence

Read in `intelligence/*.py`, `skills/plan.py`, `session.py`.

| Key | Default | Effect |
|---|---|---|
| `max_recent_actions` | `20` | Recent-actions bound (minimum 1) |
| `max_recent_results` | `20` | Recent-results bound |
| `max_failures` | `10` | Failure-history bound |
| `max_conversation_turns` | `10` | Conversation-turn bound |
| `working_memory_max_chars` | `400` | Working-memory text cap |
| `working_memory_max_facts` | `3` | Working-memory fact cap |
| `episode_retrieval_k` | `3` | Past episodes retrieved |
| `episode_retrieval_threshold` | `0.5` | Minimum similarity |
| `experience_enabled` | `true` | Past-experience context for the planner |
| `experience_max_episodes` | `3` | Episodes surfaced per run |
| `experience_context_max_chars` | `4000` | Cap on that block |
| `context_memory_enabled` | `true` (code-only) | Reference resolution memory |
| `context_max_items` | `20` (code-only) | Context memory bound |
| `context_confidence_threshold` | `0.6` (code-only) | Auto-resolve confidence |
| `context_consequential_confidence_threshold` | `0.85` (code-only) | Stricter bar for consequential calls |
| `personalization_confidence_threshold` | `0.6` (code-only) | Stored-preference match bar |
| `context_block_max_chars` | `400` (code-only) | Planner context block cap |
| `proactive_enabled` | `true` | Proactive notifications |
| `proactive_cooldown_seconds` | `300` | Per-fingerprint cooldown |
| `proactive_max_notifications` | `3` | Max per window |
| `proactive_window_minutes` | `15` | Window length |

#### voice

Read in `voice/__init__.py`, `voice/conversation.py`, `gui/app.py`, `daemon.py`.

| Key | Default | Effect |
|---|---|---|
| `enabled` | `true` | Voice master switch (also reported by `/health`) |
| `activation_hotkey` | `ctrl+alt+v` | Press-to-talk hotkey |
| `max_speech_chars` | `200` | Cap on spoken response length |
| `follow_up_enabled` | `false` | Open a no-wake-word window after FRIDAY speaks |
| `follow_up_timeout_s` | `10.0` | Length of that window |
| `confirm_listen_timeout_s` | `12.0` | Listen time for a spoken confirmation |
| `post_tts_cooldown_s` | `0.4` | Pause after speech before listening |
| `wake_sound_enabled` | `true` | Chime on wake |
| `wake_sound_path` | `""` | Custom chime WAV; blank uses the bundled one |
| `wake_sound_warmup_s` | `0.85` | Capture warm-up after the chime |
| `stt.model` / `language` / `device` / `compute_type` / `beam_size` | `base` / `en` / `cpu` / `int8` / `1` | faster-whisper settings |
| `stt.vad_filter` | `false` | faster-whisper internal VAD |
| `stt.initial_prompt` | `""` | Whisper initial prompt |
| `stt.condition_on_previous_text` | `true` | Whisper decoding option |
| `stt.vocabulary` | list of 10 names (FRIDAY, VS Code, ...) | Decode-time hotword hints |
| `stt.silence_timeout_s` / `max_recording_s` / `silence_rms_threshold` / `min_speech_s` / `pre_roll_ms` | `0.8` / `15` / `0.012` / `0.25` / `300` | Recording and end-of-speech tuning |
| `stt.vad_enabled` / `vad_threshold` / `vad_sustain_threshold` | `true` / `0.5` / `0.35` | Silero VAD gating |
| `stt.input_device` | `null` | Microphone device index; null = system default |
| `tts.enabled` | `true` | Speak replies |
| `tts.voice_id` / `rate` / `volume` | `""` / `null` / `null` | SAPI voice settings; blank/null = engine default |
| `wakeword.enabled` | `true` | Always-listening wake word |
| `wakeword.model` | `hey_jarvis` | openWakeWord pretrained model name |
| `wakeword.threshold` | `0.6` | Detection confidence |
| `wakeword.models_dir` | `""` | Blank = `data/models/openwakeword`; the model is downloaded on first use (needs internet once) |
| `wakeword.cooldown_s` | `1.5` | Cooldown after an interaction |
| `wakeword.window_s` / `persist_frames` / `debug` | `1.0` / `2` / `false` (code-only) | Rolling-window decision and verbose diagnostics |
| `wakeword.verifier_model_path` | code `""`; shipped `config.yaml` sets `data/models/wake_verifier.pkl` | Optional scikit-learn verifier; blank disables |
| `wakeword.verifier_threshold` | `0.1` | Base-model score needed before the verifier runs |

#### gui

Read in `gui/app.py`, `gui/main_window.py`, `gui/state_hub.py`.

| Key | Default | Effect |
|---|---|---|
| `start_minimized` | `false` | Start in tray |
| `always_on_top` | `false` | Keep window on top |
| `self_state_poll_ms` / `task_state_poll_ms` / `telemetry_poll_ms` / `desktop_context_poll_ms` | `300` / `750` / `2000` / `4000` | State poll intervals |
| `desktop_context_include_ocr` | `false` | OCR in the desktop-context panel |
| `error_hold_s` | `4.0` | How long the core widget shows an error look |

#### logging

| Key | Default | Effect | Source |
|---|---|---|---|
| `level` | `INFO` | Root log level | `log.py` |

### 5b. Keys in `config.yaml` that no code reads

A search of `friday/` found these only in `friday/config.py` (definition) and `config.yaml`; no code reads them. Changing them has no effect.

| Key | Notes |
|---|---|
| `voice.save_recordings` | Comment says raw recordings are never kept unless enabled, but no code reads the flag. |
| `voice.tts.engine` | Only the SAPI adapter exists; `TtsEngine` is constructed without this value. |
| `voice.wakeword.engine` | Only openWakeWord exists; `WakeWordDetector` is constructed without this value. |
| `identity.user_name` | Nothing uses it; FRIDAY does not address you by name from this setting. |

Related clarifications:

- `identity.wake_word` does **not** control the audio wake word. The audio wake word is the openWakeWord model named by `voice.wakeword.model`, which defaults to `hey_jarvis` (the phrase "hey jarvis"). `config.yaml` itself notes there is no pretrained "FRIDAY" model. `identity.wake_word` only affects typed-text normalisation (strip a leading phrase).
- `voice.wakeword.verifier_model_path` is set to `data/models/wake_verifier.pkl` in the shipped `config.yaml`. `data/` is git-ignored, so that file is **absent on a fresh clone**. The detector is built with `verifier_model_path or None`; what happens when a configured path does not exist was not verified in detail. `friday/voice/wakeword.py` has a log message referencing this key at line 173, which suggests a missing or unloadable verifier is handled there; confirm in that file. To disable the verifier explicitly, set the key to `""`.
- Keys that exist in `config.yaml` but whose source usage was only checked at the `CFG` access level (not for subtle behaviour) are marked as read in 5a; the "Effect" column paraphrases the comments in `config.py`/`config.yaml`, not an end-to-end behavioural test.

## 6. Notes and limits

- Nothing here claims that the defaults are safe for every setup. In particular, local-mode daemon access is open to every program on the machine, snapshots are unencrypted, and log redaction is best-effort.
- This page was written from reading source only; no code was run.
