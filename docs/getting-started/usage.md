# Using FRIDAY

This page describes how to start FRIDAY, how a request flows through it, what
the permission tiers do, how voice and hotkeys work, and what STOP does and does
not do. Every statement was read from the source files named in it. Nothing here
was reproduced at runtime for this page, and nothing here is a claim that
FRIDAY is production-ready or that its output is free of sensitive data. Items
that could not be confirmed from source are marked **unverified**.

## 1. Entry points

All commands go through `python run.py <command>` (`run.py` calls `friday.cli.main`).
A bare `python run.py` with no arguments is turned into `python run.py console`
(`friday/cli.py`, `main`).

`run.py`'s own docstring lists only `console` (bare), `say`, `serve`, `skills` and
`audit`. The CLI actually registers more commands, listed below.

| Command | What it does (from `friday/cli.py`) |
|---|---|
| `desktop` | Starts the Qt GUI: window, tray icon, global hotkeys, voice. |
| `console` | Interactive terminal loop (default). Starts the scheduler and trigger watcher in the same process. |
| `say "TEXT" [--yes/-y]` | Runs one utterance through the same session, prints the reply, exits with code 0 if the result was ok and 1 otherwise. `--yes` auto-approves every confirmation. Does not start the scheduler or triggers. |
| `serve` | Runs the HTTP/WebSocket daemon (section 1.3). Also starts the scheduler and trigger watcher. |
| `skills` | Lists registered skills that are not marked internal. |
| `audit [--limit N]` | Shows recent audit rows (default 20). |
| `jobs` | Lists scheduled and event-driven jobs. |
| `recovery [list\|recover ID\|cleanup\|migrate] [--limit/-n N] [--yes/-y]` | Operator commands for the recovery copies FRIDAY keeps before overwriting or deleting a file. `list` is read-only. `recover`, `cleanup` and `migrate` ask for confirmation first. They run through the normal executor, so permission, confirmation and audit apply. |
| `db [status\|migrate\|scan\|scrub\|compact\|verify-backup\|restore]` | Database placement and scrub tooling. Options: `--target active\|legacy`, `--apply`, `--confirm CODE`, `--backup-dir`, `--allow-synced`, `--backup FILE`, `--to NEWFILE`. `scrub` is a preview unless `--apply --confirm CODE` is given with an explicit `--target`. The code comes from the preview for that target. Per the CLI help, FRIDAY must not be running when you apply a scrub or migrate. |

Inside the console loop, typed words handled before the normal pipeline are:
`quit`/`exit`/`q`, `skills`, `audit`, `jobs`/`schedule`/`automations`,
`recovery ...`, and `teach <skill>` (binds the previous utterance to a skill).
Everything else goes to `SESSION.handle(text, actor="text")`.

The console loop is sequential: it awaits each `handle` call before reading the
next line. Per the code read, you therefore cannot type `stop` at that prompt
while a goal is still being handled. Use another process or the GUI for that
(see section 5). This is read from the loop structure, not run.

### 1.1 Windows launcher

`START.bat` checks for `.venv\Scripts\pythonw.exe` and, if present, starts
`run.py desktop` with it, in the background. If the virtual environment is
missing it prints the commands to create it and exits with code 1.

### 1.2 GUI, console and daemon are separate processes

- `desktop`, `console`, `say` and `serve` each create their own in-process
  `SESSION`, executor, intent state and event bus. They do not share a running
  goal, a pending confirmation, or the stop flag.
- Voice, wake word and the global hotkeys exist only in the GUI process
  (`friday/gui/app.py`). `console`, `say` and `serve` do not start them.
  `/health` on the daemon reports `voice` from `CFG.voice.enabled`, which is a
  config flag, not evidence that a microphone pipeline is running.
- The GUI refuses a second instance (`single_instance.acquire`).

### 1.3 Daemon endpoints

`python run.py serve` listens on `CFG.daemon.host:CFG.daemon.port` (`friday/daemon.py`).

| Endpoint | Behavior |
|---|---|
| `GET /health` | `ok`, version, skill count, `voice` config flag, and the kind of pending question if any. |
| `GET /skills` | Non-internal skills with tier, params, examples. |
| `POST /say` `{text, actor="text"}` | Natural language in, through `SESSION.handle`. |
| `POST /invoke` `{skill, args, actor="text"}` | Calls `EXECUTOR.run` directly: skips intent matching, not the permission guard. |
| `POST /cancel` | Calls `SESSION.stop(source="daemon")`. |
| `POST /teach` `{skill, utterance?}` | Binds an utterance, or the last one, to a skill. |
| `GET /audit?limit=N` | Recent audit rows. |
| `WS /ws` | Send `{"text": ..., "actor": ...}`; receive every bus event and a `result` message. |

Things to know, as read from source:

- `/invoke` looks the skill up by name in the registry. The route does not
  filter out skills marked internal, which `/skills` and the CLI table hide. It
  does not check that the caller is who `actor` says. The **client supplies the
  actor** (default `"text"`; `/say` and `/ws` accept it too). The permission
  decision uses that string. For example, `scheduler` and `trigger` are treated as
  unattended (section 3), and any other string is treated as attended.
- `/cancel` calls `SESSION.stop` in the daemon's own process. It cannot stop a
  goal running in a separate GUI or console process, because each process has its
  own intent state.
- Network guard (Phase 62/63 comments in `daemon.py`): the default host is
  loopback; a non-loopback host refuses to start unless `daemon.allow_remote` is
  set and a token of at least 32 characters is in the `FRIDAY_DAEMON_TOKEN`
  environment variable. Requests with a non-loopback `Host`, or a browser
  `Origin` that is not the daemon's own, are refused with 403. The source's own
  comment says that in local mode this does not authenticate anyone: another
  program on the same machine has full access.

## 2. Main interaction flow

Typed or spoken text ends up in `Session.handle(text, actor=...)`
(`friday/session.py`). Voice text reaches it through `backend.ask(text, actor="voice")`
in the GUI; typed text in the GUI command bar uses `actor="text"`.

In order, `handle` does the following:

1. Empty text returns "I didn't catch that."
2. If the text is a stop phrase **and** something is stoppable, it calls
   `Session.stop` and returns (section 5).
3. It checks, in order: a pending question (confirmation, missing slot,
   clarification, goal clarification); the exact recovery sentences
   (`recoveryroute`); "yes, fix it" scope expansion after a read-only goal;
   "read more"/"next page"; and "do that again".
4. `BRAIN.understand(text)` matches the utterance to a skill and extracts
   arguments. A bare affirmation with nothing to affirm gets a clarifying
   question instead of a skill.
5. Compound sentences ("open X and then do Y") that match one skill for the
   first clause can be rerouted to `plan.run`. A vague, diagnostic-style request
   that matched nothing can also be routed to `plan.run`, when the planner is
   enabled.
6. The direct-route gate (`friday/directroute.py`, section 2.1) decides whether
   a single-skill match may run as it stands. If not, the request becomes a
   `plan.run` goal.
7. `_act` runs the result. A match runs the skill; a missing argument asks you
   for it; ambiguity asks you to choose; no match says it does not know.

### 2.1 Direct skill execution vs `plan.run`

| | Direct skill | `plan.run` (planner) |
|---|---|---|
| Chosen when | One skill matches and the direct-route gate finds no objection. | Compound or vague request, or the gate rejects the direct route. Also resumes a clarified or expanded goal. |
| Runs through | `run_verified` (`friday/orchestrator.py`) which wraps `EXECUTOR.run`. | `EXECUTOR.run("plan.run")`, which runs an orchestrator loop. Each step is again a skill call through `EXECUTOR`. |
| Decision-maker | Embedding match plus regex argument extraction. | A local LLM picks the next tool one step at a time. |
| Direct-route gate | Applies. It checks for a dropped path/URL/quoted operand, a whole sentence passed as an argument, a second instruction in an argument, a compound request, several facts for a one-fact skill, a read-only skill for a change request, two near-tied skills, and a few memory/forget checks. | Not applicable. |
| Intent guard | Not applied. | Applies (`planner.intent_guard`): the tool must fit the kind of action your words asked for. Checked before permission and confirmation. |
| Tool-family check | Not applied. | Applies (`planner.tool_family_guard`): browser vs UI-automation tool discipline. |
| Read-back verification | Applies for state-changing skills that have a verifier (`run_verified`). | Applies per step, plus a goal-level verification summary. |
| Permission, confirmation, audit | Always, through `EXECUTOR`. | Always, per step, through `EXECUTOR`. |
| Limits | None beyond the skill's own. | `max_steps: 16`, `step_timeout_s: 90`, `total_timeout_s: 300` (`config.yaml` `planner`), `max_replans: 2`. |

How `run_verified` words its result:

- **Verified:** the result is returned as the tool gave it.
- **Failed read-back:** `ok` is set to false and the reply says it did not take effect.
- **Unverified or partial:** `ok` is left alone and the reply adds a note that
  the effect could not be confirmed.
- **Unverifiable skills:** `friday/verify.py` keeps an explicit `UNVERIFIABLE`
  table of skills for which no read-back exists; the honest answer for those is
  "unverified". Which skills are in that table was not enumerated for this page.
- **Read-only, refused or already-failed calls:** returned untouched.

`plan.run` is skill-tier L1 and is itself called through `EXECUTOR.run` directly, not
`run_verified`. For the planner, `done` is only a suggestion: a comment in
`orchestrator.py` says it is never the verdict, and success is decided afterwards
by the evaluator on the real observations. A `done` after nothing but failed
steps is reported as a failure. Summaries are also checked against what the
steps actually returned (answer grounding), but that check is rule-based and
the source lists known limitations. Treat a planner summary as a claim to check
against the audit log, not as proof.

## 3. Permission tiers

Defined in `friday/permissions.py`, defaults in `config.yaml`:

| Tier | Meaning (docstring) | Default policy |
|---|---|---|
| L0 | Read-only: list, read, search, screenshot | `auto` |
| L1 | Reversible write: open app, move file, set volume, type text | `auto` |
| L2 | Destructive: delete, overwrite, kill process, registry write | `confirm` |
| L3 | External or irreversible: send message, purchase, shutdown, credentials | `confirm` |

- Policies are `auto`, `confirm` or `deny`. `permissions.overrides` sets a
  per-skill policy and beats both the tier default and a risk escalation.
- A skill can have a risk classifier that raises one specific call to L3. A
  classifier that raises an error is treated as L3.
- Because L1 is `auto`, L1 actions run without asking. Setting `overrides` for
  a skill to `deny` or `confirm` is how to change that for one skill.
- **Confirmation:** the session parks the question and waits up to **60 seconds**
  (`asyncio.wait_for(future, timeout=60)` in `Session._confirm`). Timeout counts as no.
  Your next utterance resolves it: a word from the yes list approves, a word from
  the no list declines, and anything else declines it and is treated as a new command.
  The `say` and `recovery` CLI commands and the console loop install a terminal
  `approve? [y/N]` prompt instead (`_install_terminal_confirm`).
  With no confirm handler wired, the executor refuses.
- **Unattended actors** `scheduler` and `trigger` (`UNATTENDED` in `permissions.py`):
  - a call above `permissions.unattended_ceiling` (default **L1**) is denied;
  - a call whose policy is `confirm` is denied, because nobody is present to answer.
- A denied call raises `PermissionError_`, is audited as `blocked`, and the user
  hears "I'm not allowed to do that". A declined confirmation is audited as `denied`.
- `shell.run` is declared tier L2 (`friday/skills/shell.py`), so it asks first by default.
  `routine.run` is declared L1.

The credential check mentioned in the executor (`safe_speech`, `scrub_tree`) is applied
to **failed** results and to exception text. A successful reply is deliberately left
alone, so successful output is not guaranteed to be free of sensitive values.

## 4. Voice and hotkeys (GUI process only)

| Hotkey | Action | Source |
|---|---|---|
| Ctrl+Alt+Space | Show/hide the command bar window | `HOTKEY` in `friday/gui/app.py` |
| Ctrl+Alt+V | Push-to-talk (`voice.activation_hotkey`) | `config.yaml` |
| Ctrl+Alt+X | Stop the running goal (`permissions.stop_hotkey`; blank disables it) | `config.yaml`, `app.py` |

- Hotkeys use Win32 `RegisterHotKey`. If registering one fails (for example another
  app owns it, or a duplicate FRIDAY process), `friday/hotkey.py` only logs a
  warning and records the combo as not registered. FRIDAY keeps running. The GUI does
  not stop or warn you in any other way that I found. **Unverified:** whether any
  on-screen indicator exists; only the log was read.
- All hotkeys must be bound before `HOTKEYS.start()`; the GUI does this in order.
- **Wake word:** engine `openwakeword`, `voice.wakeword.enabled: true` by default,
  model **`hey_jarvis`**, threshold 0.6. The phrase is **not** "FRIDAY": the
  config comment says there is no pretrained "FRIDAY" model and this is a
  placeholder phrase. The pretrained model is downloaded once and needs
  internet that one time, then runs offline (per the config comment).
- **Verifier model:** `voice.wakeword.verifier_model_path` defaults to
  `data/models/wake_verifier.pkl`. `data/` is listed in `.gitignore`, so on a
  fresh clone that file is expected to be absent. I did not look inside `data/`.
  `friday/voice/wakeword.py` raises an error when a configured verifier file is
  missing. The conversation loop catches that kind of error, logs
  "wake-word detector unavailable ... (Ctrl+Alt+V is unaffected)", marks wake word
  as unavailable and retries with backoff (up to 60 s). To avoid the repeated
  failure, set `verifier_model_path` to blank (the config comment says blank means
  pretrained-model-only) or train one with `scripts/train_wake_verifier.py`.
  **Unverified:** that exact fresh-clone failure was not run.
- Ctrl+Alt+V is bound separately from the wake-word loop, so push-to-talk is meant to
  work even when the wake word cannot start. If the whole conversation loop fails to
  build, the GUI falls back to a hotkey-only voice session; if that fails too, voice is
  skipped. `START.bat`'s comments say the same.
- With `follow_up_enabled: false` (default) each command needs its own wake word or
  Ctrl+Alt+V press.
- Spoken replies are capped at `voice.max_speech_chars` (200). Raw recordings are
  not kept unless `voice.save_recordings` is turned on.
- A spoken or typed "stop" in the GUI goes through `Session.handle` like any text
  (section 5). Whether the microphone is picking up speech correctly depends on
  your hardware; accuracy was **not** verified for this page.

## 5. STOP

### 5.1 What triggers it

All of these end in `Session.stop` (`friday/session.py`):

- The STOP button in the GUI command bar (`Backend.stop(source="gui")`).
- The global hotkey Ctrl+Alt+X (`Backend.stop(source="hotkey", wait=False)`).
- A typed or spoken stop phrase. The list is exact: `stop`, `stop it`, `stop that`,
  `stop this`, `stop now`, `stop everything`, `stop all`, `stop the task`, `stop the
  goal`, `stop the plan`, `stop doing that/this`, `stop what you are doing`, `cancel`
  and `cancel it/that/this/the task/the goal/the plan/everything`, `abort` and
  `abort it/the task/the goal`, `halt`, `emergency stop`. The filler words `hey`,
  `friday`, `please`, `now` are ignored at either end. "Stop the music" is not a stop phrase.
- `POST /cancel` on the daemon (in the daemon's process only).

### 5.2 When it does anything

`Session.stop` sets the cancel flag only when a **`plan.run` goal is running**
(`INTEL.goal_running`; `request_cancel` returns False and arms nothing otherwise). It also
declines a confirmation that is currently waiting. A typed or spoken stop phrase is only
treated as a stop at all while a goal is running or a confirmation is pending
(`_stop_applies`); otherwise it is routed like any other text.

- Goal running and/or confirmation waiting: "Stopping. No further steps will start."
  (plus a note if a confirmation was declined).
- Neither: **"There's nothing running to stop."**

Nothing already done is reversed. A stop is recorded in the audit log as `friday.stop`.

### 5.3 What it does not interrupt (read from source, not reproduced)

Because the flag is only armed for a running `plan.run` goal, and no other mechanism
is wired to it:

- A direct `shell.run` typed as a normal command, with no goal active, is **not**
  interrupted by STOP. `shell.py` only wires the stop callback when `INTEL.goal_running`
  is true (`_stop_scope`).
- `routine.run`, `schedule.run_now`, scheduled jobs and trigger jobs are **not**
  interrupted by STOP when no goal is active. I found no stop wiring for them in the files
  read. **Unverified for `schedule.run_now` specifically**: I did not read its source.
  If they run a `plan.run` goal internally, that goal could be stoppable; this was not checked.
- A step running in a worker thread cannot be interrupted. It is abandoned or finishes,
  and its audit row is marked "outcome unknown" until the thread ends (Phase 53 comments in
  `permissions.py`).
- The daemon's `/cancel` does not reach a goal running in the GUI or console process.

### 5.4 Shell commands inside a goal

For `shell.run` inside a running goal, `friday/shellproc.py` runs the command in a
Windows job object. On a stop request or the command timeout it terminates the process
tree. Possible results are reported as stopped, timed out, or "stop acted on but not
confirmed" (unknown outcome).

Two limitations read from source:

1. **Descendant holding the pipe.** In `shellproc.run_owned` (around lines 375-394), the
   loop checks the stop flag and the deadline only after `proc.communicate(timeout=...)`
   times out. If the root shell has exited but a descendant keeps the output pipe open,
   the code reaches `if proc.poll() is not None:` and returns `COMPLETED`. In that branch
   it does not terminate the process tree, and the stop or timeout is discarded. A command
   in this situation can therefore be reported as completed while a descendant may still
   be running. Not runtime-reproduced.
2. **Misleading summary after a stopped shell command.** `orchestrator._cancelled_summary`
   decides to say "was already running and can't be interrupted, so it ran to completion"
   from the `finished_after_stop_request` flag on the observation. It does not look at the
   observation's `data.status`. So after a shell command that STOP actually killed inside
   a goal, the goal summary can wrongly say the step ran to completion. Not
   runtime-reproduced.

Treat the audit log (`python run.py audit`) as the record of what happened, and check the
machine yourself after a stop.

## 6. Where to look when something goes wrong

- Log file: `data/logs/friday.log` (named in `START.bat`; not opened for this page).
- `python run.py audit` and `python run.py jobs`.
- For the recovery copies kept before an overwrite or delete: `python run.py recovery list`.

## Claims not verified

- Whether the wake-word verifier file is actually absent on any given machine
  (inferred from `data/` being gitignored; `data/` was not opened).
- That a missing verifier produces exactly the logged fallback described (read from code,
  not run), and that wake-word model download works for you.
- Whether any on-screen warning appears when a hotkey fails to register.
- Whether `schedule.run_now`, `routine.run` and scheduled or trigger jobs start a stoppable
  `plan.run` goal in any case (their source was not read).
- Which skills are in `verify.UNVERIFIABLE`.
- That the shellproc pipe-holding case and the `_cancelled_summary` wording problem occur at
  runtime (source-read only).
- Voice transcription accuracy and wake-word reliability on real hardware.
- Behavior of `START.bat` beyond what its text says.
