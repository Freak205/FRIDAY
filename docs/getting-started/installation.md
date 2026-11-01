# Installation

> **Status: this procedure has not been tested on a clean machine.** It is
> assembled from `START.bat`, `requirements.txt`, `config.yaml` and the source.
> One known dependency conflict (below) may make a fresh `pip install` fail.

## Requirements

- **Windows** (the code uses DPAPI, `RegisterHotKey`, Win32 job objects, COM
  audio/brightness control and SAPI text-to-speech). Other platforms are not
  supported.
- **Python 3.12.** The reference environment is 3.12.10. Other versions have not
  been tried.
- **Ollama** running locally, with the model named in `config.yaml`
  (`llm.model`, currently `qwen2.5:3b`, at `http://localhost:11434`).
- Network access on first run, for model downloads (see below).
- A microphone and speakers only if you want voice.

## 1. Create the virtual environment and install dependencies

From the repository root:

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

These are the commands `START.bat` prints when the venv is missing; they are the
only install instructions in the repository.

### Known dependency conflict (unverified outcome)

`requirements.txt` pins `Pillow==11.1.0`, while the pinned `fastembed==0.5.1`
declares `pillow>=10.3.0,<11.0.0` in its package metadata. The working
development environment has Pillow 10.4.0. `pip` may therefore refuse to resolve
the file as written. This has not been reproduced on a fresh install. If it
fails, installing a Pillow version below 11 is the likely workaround, but that
is untested.

A few packages are used by the code but are not listed directly in
`requirements.txt` (for example numpy, onnxruntime); they are expected to arrive
as dependencies of listed packages.

## 2. Install Ollama and pull the model

Install Ollama from <https://ollama.com>, make sure it is running, then:

```bat
ollama pull qwen2.5:3b
```

Nothing in the Python code downloads this model. `scripts/launch_friday.ps1`
pulls a hard-coded model name and ignores `config.yaml`; if you change
`llm.model`, pull the model you configured yourself. `START.bat` does not check that Ollama is running, and
behavior with Ollama stopped has not been verified.

## 3. Optional components

- **Tesseract OCR** (binary, plus the `pytesseract` wrapper already in
  `requirements.txt`): only needed for screen-text features.
- **Playwright browser**: the browser skills use Playwright. If Chrome is not
  installed, run `.venv\Scripts\python.exe -m playwright install chromium`.
- **WhatsApp skills**: tested only against a mock page, never the live site.
- **Wake-word verifier model**: see "Fresh-clone limitations".

## 4. First-run downloads

The first run fetches models over the network (cached locally afterwards):

- `BAAI/bge-small-en-v1.5` via fastembed (intent matching; startup waits for it)
- faster-whisper `base` (speech to text)
- openWakeWord models and the Silero VAD model (wake word and voice activity)

Expect a delay and a network requirement on the first launch. Exact download
sizes have not been measured.

## 5. Launch

| Method | What it does |
|---|---|
| `START.bat` | Starts `pythonw run.py desktop` (GUI, tray icon, hotkeys, voice). It does not check that Ollama is running. |
| `scripts\launch_friday.vbs` | Starts Ollama first, then `START.bat`. |
| `.venv\Scripts\python.exe run.py desktop` | GUI with a visible console, useful for diagnosing start-up. |
| `.venv\Scripts\python.exe run.py console` | Text-only console; no GUI, no voice, no hotkeys. |
| `.venv\Scripts\python.exe run.py serve` | HTTP/WebSocket daemon (see [usage](usage.md)). |

`python run.py` with no argument runs `console`. See [usage](usage.md) for all
commands and [configuration](configuration.md) for settings.

## Fresh-clone limitations

- **The wake word will not start on a fresh clone.** `config.yaml` points the
  personal wake-word verifier at `data/models/wake_verifier.pkl`. `data/` is
  gitignored, so the file does not exist after cloning, and wake-word listening
  fails to start (it retries with back-off and logs the reason to
  `data/logs/friday.log`). FRIDAY still starts, and push-to-talk (Ctrl+Alt+V)
  still works. Options are to leave wake-word listening unused, to blank the
  `verifier_model_path` setting, or to train a verifier with
  `scripts/train_wake_verifier.py`. Whether blanking the setting gives a
  working generic wake word has not been verified here.
- **The audio wake phrase is `hey_jarvis`**, not "FRIDAY".
- **No clean-machine install has been performed**, and nothing here has been
  verified by a launch from scratch.
- **No human validation record exists.** `MANUAL_VALIDATION.md` is an unticked
  checklist.
- Runtime data (logs, browser profile, caches) is written under `data/` inside
  the repository. If the repository is in a cloud-synced folder, that data
  syncs. The database and snapshots are stored elsewhere; see
  [configuration](configuration.md).
