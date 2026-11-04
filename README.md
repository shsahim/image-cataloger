# Image Cataloger

A desktop app (and command-line tool) that writes a one-line description of **every image in a folder**, using Claude's vision models. Point it at any folder, local or a mounted network share, choose how many parallel threads to run, and press **Start**. You get a CSV and a JSON file with one row per image:

| column | meaning |
|---|---|
| `path` | absolute path of the file |
| `kind` | `image` or `video` |
| `summary` | one factual line (under ~20 words): subject, setting, legible text, screenshot vs photo; videos start with `Video, 12s:` |
| `width`, `height`, `bytes` | original pixel size and file size |
| `duration_s` | video length in seconds (empty for images) |
| `md5`, `sha256` | content hashes |
| `duplicate_of` | for byte-identical files, the path of the copy that was described |
| `status` | `ok`, `duplicate`, `unreadable`, `error`, or `pending` (not done yet) |
| `error` | why a file is unreadable or errored |
| `model` | which model wrote the summary |

Runs are **resumable**: everything is written to a SQLite database as it happens, so if you stop the app, lose the network, or the machine crashes, the next Start picks up exactly where it left off and never pays to describe the same file twice.

Designed for accuracy first, then cost and speed:

- **Byte-identical duplicates are hashed and described once.** The copies are linked through `duplicate_of` and inherit the summary. A *Dedupe only* mode does just this step, with no API calls.
- **Images are downscaled in memory** to at most 1568 px on the long side and re-encoded as JPEG (quality 85) before upload. Claude never uses more than that, so this keeps description quality while cutting image tokens sharply. Source files are never modified.
- **Several images per request** (default 6). The model must answer with a JSON array of exactly one line per image; if the count does not match, that batch is re-asked one image at a time, so a malformed answer can never shift descriptions onto the wrong files.
- **Worker pool you control**, exponential backoff on rate limits (429) and overloads (529), a per-request timeout, and per-file error recording, so one bad file or one busy minute never aborts a run.
- Files Pillow cannot decode are flagged `unreadable` rather than crashing.
- **Videos** (mp4, mov, m4v, avi, mkv, webm, mpg, wmv, 3gp, mts, flv) are hashed and deduplicated like images. For descriptions, a few frames (5 by default) are sampled evenly through the clip with ffmpeg and sent as one request; the answer is one line for the whole video, prefixed with its length. ffmpeg comes bundled through `imageio-ffmpeg`, so nothing extra to install; a system ffmpeg is used when present.

## Install

Requires Python 3.11 or newer.

> **macOS note:** put the project and its virtual environment on a path containing only plain ASCII characters (for example `~/Projects/image-cataloger`). Qt, the GUI toolkit, cannot load its platform plugins from a folder whose name contains characters such as a curly apostrophe (`Spencer’s MacBook Pro`). The app detects this and tells you.

```bash
git clone git@github.com:shsahim/image-cataloger.git ~/Projects/image-cataloger
cd ~/Projects/image-cataloger
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[heic,dev]"
```

`[heic]` adds HEIC/HEIF support (iPhone photos) through `pillow-heif`. `[dev]` adds pytest.

## API key

The app calls the Claude API directly, so it needs an Anthropic API key (create one at <https://console.anthropic.com/> under *API keys*).

- In the GUI, click **API key…**, paste it, and **Save**. The key is stored in your operating system's keychain (macOS Keychain, Windows Credential Manager, Secret Service on Linux) through the `keyring` library. It is never written to the repository, a config file, or the catalog outputs.
- Alternatively set the environment variable `ANTHROPIC_API_KEY`. The environment variable takes precedence when both exist.

If you have a Claude subscription with [Claude Code](https://claude.com/claude-code) installed but no API key, choose the **Local Claude Code CLI** engine. It shells out to `claude -p` and lets Claude read the files itself. It works, but it is slower and gives you less control than the API engine, which is the primary path.

## Run the GUI

```bash
image-cataloger
```

(or `python -m image_cataloger`). Then:

1. **Browse…** to the folder. Network shares mounted under `/Volumes/...` (macOS) or a mapped drive (Windows) work like any other folder. Tick *Include subfolders* for a recursive scan and *Include videos* to catalog video files too (*Frames per video* sets how many frames are sampled per clip; the Local CLI engine cannot describe videos).
2. Pick the engine and model. `claude-haiku-5-5` is the default: fast and very cheap. `claude-sonnet-5-5` reads fine detail and small text better; `claude-opus-5-5` is the most accurate and the most expensive.
3. Set **Threads** (parallel requests). Start at 4 to 8; raise it until the log starts showing rate-limit retries, then back off a little.
4. Press **Start**. Watch files done / total, images per minute, ETA, running token and cost figures, and the error count. **Pause** lets in-flight requests finish and holds the rest; **Resume** continues; **Stop** ends the run after in-flight requests and writes the CSV/JSON with everything done so far.
5. Press **Open CSV** when it finishes.

**Dedupe only.** Tick *Dedupe only* (or pass `--dedupe-only` on the command line) to hash the tree and link byte-identical files without describing anything. Nothing is sent to the model and nothing is deleted. You get the normal CSV/JSON with `duplicate_of` filled in and `status` `pending`, plus `<folder name>_catalog_duplicates.csv` listing each duplicate next to the copy it matches, its size and hash, so you can decide what to remove. Running again later with the box unticked describes the pending files against the same database, so the hashing is not repeated.

Outputs default to `<folder name>_catalog.csv`, `.json` and `.sqlite` next to the chosen folder (or in `~/ImageCataloger` if that location is read-only). **Change…** picks another location. Re-running against the same outputs resumes; files that errored are retried, files already described are skipped, and files that changed on disk are re-hashed and re-described.

## Run headless

```bash
image-cataloger-cli "/Volumes/share/Recovered Photos" --workers 8 --batch-size 6 --model claude-haiku-5-5
```

`image-cataloger-cli --help` lists every option (`--dedupe-only`, `--no-videos`, `--video-frames`, `--no-recursive`, `--engine cli`, `--effort`, `--max-side`, `--csv`, `--json`, `--db`). Ctrl-C stops cleanly and keeps the progress.

## What it costs

Standard API prices (November 2026): Haiku 5.5 $0.10 / $0.50, Sonnet 5.5 $2 / $10, Opus 5.5 $4 / $20 per million input / output tokens.

A photo downscaled to 1568 px on its long side costs about 2,500 image tokens at most (fewer for smaller or narrower images), plus roughly 100 tokens of prompt and answer per image. For **1,000 typical photos** that works out to:

| model | approx. cost per 1,000 images |
|---|---|
| `claude-haiku-5-5` | about $0.30 |
| `claude-sonnet-5-5` | about $6 |
| `claude-opus-5-5` | about $12 |

These are upper-bound estimates for large photos; screenshots and small images cost proportionally less. A video costs about as much as the number of frames sampled from it (5 frames ≈ 5 images). The GUI shows the actual running cost computed from the token counts the API reports. The *Effort* setting (`low` by default) controls how much the model thinks before answering; `low` is right for captions.

Throughput scales with the thread count. One request of 6 images takes a few seconds on Haiku, so 8 threads sustains several hundred images per minute until you hit your account's rate limit.

## How it works

```
scan folder ──> hash (md5+sha256) + read dimensions ──> link duplicates
      │                                                     │
      └───────> SQLite (one row per file, status) <─────────┘
                        │
            pending files, in batches of N
                        │
        worker 1 … worker K (your thread count)
          downscale ─> Claude Messages API ─> parse JSON array
          (count mismatch? re-ask one by one)  ─> write rows
                        │
                 CSV + JSON export
```

The engine lives in `src/image_cataloger/` (`scanner`, `hashing`, `imaging`, `db`, `parsing`, `engines/`, `runner`, `export`) and has no GUI dependency; `cli.py` drives it from a terminal and `gui/` wraps it in PySide6.

## Tests

```bash
pytest
```

The tests cover hashing and duplicate detection, downscaling (orientation, transparency, truncated files), video probing and frame sampling on an ffmpeg-generated clip, response parsing, the API engine's retry / mismatch / refusal handling against a mocked client, and resume behaviour of the runner (stop, re-run, errors retried, changed files re-described).

## License

MIT. See `LICENSE`.
