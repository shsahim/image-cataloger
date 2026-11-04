"""Headless command-line entry point: `image-cataloger-cli FOLDER [options]`."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .config import RunConfig, get_api_key
from .engines.base import RunControl
from .pricing import API_MODELS, DEFAULT_MODEL
from .runner import CatalogRunner, Progress, build_engine


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="image-cataloger-cli",
        description="Describe every image in a folder with one line each. Resumable; safe to re-run.",
    )
    ap.add_argument("folder", help="folder to catalog (local path or mounted share)")
    ap.add_argument("--no-recursive", action="store_true", help="only the top-level folder")
    ap.add_argument("--dedupe-only", action="store_true",
                    help="hash files and link byte-identical duplicates, then export; no model calls")
    ap.add_argument("--no-videos", action="store_true", help="skip video files")
    ap.add_argument("--video-frames", type=int, default=5,
                    help="frames sampled per video for its description (default 5)")
    ap.add_argument("--workers", type=int, default=4, help="parallel requests (default 4)")
    ap.add_argument("--batch-size", type=int, default=6, help="images per request (default 6)")
    ap.add_argument("--engine", choices=["api", "cli"], default="api",
                    help="api = Claude API (needs key); cli = local Claude Code CLI")
    ap.add_argument("--model", default=None,
                    help=f"API model id (default {DEFAULT_MODEL}; choices {', '.join(API_MODELS)}) "
                         "or, with --engine cli, haiku/sonnet/opus")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="low",
                    help="thinking effort for the API engine (default low)")
    ap.add_argument("--max-side", type=int, default=1568, help="downscale long side to N px (default 1568)")
    ap.add_argument("--quality", type=int, default=85, help="JPEG quality for the resized copy (default 85)")
    ap.add_argument("--db", default="", help="SQLite path (default: <folder>_catalog.sqlite next to folder)")
    ap.add_argument("--csv", default="", help="CSV output path (default next to folder)")
    ap.add_argument("--json", default="", help="JSON output path (default next to folder)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    model = args.model or (DEFAULT_MODEL if args.engine == "api" else "haiku")
    cfg = RunConfig(
        folder=args.folder,
        recursive=not args.no_recursive,
        workers=args.workers,
        batch_size=args.batch_size,
        engine=args.engine,
        model=model,
        effort=args.effort,
        max_side=args.max_side,
        jpeg_quality=args.quality,
        db_path=args.db,
        output_csv=args.csv,
        output_json=args.json,
        dedupe_only=args.dedupe_only,
        include_videos=not args.no_videos,
        video_frames=args.video_frames,
        api_key=get_api_key() if args.engine == "api" and not args.dedupe_only else None,
    )
    if cfg.engine == "api" and not cfg.dedupe_only and not cfg.api_key:
        print("No API key. Set ANTHROPIC_API_KEY or store one from the GUI settings dialog.",
              file=sys.stderr)
        return 2

    control = RunControl()
    signal.signal(signal.SIGINT, lambda *_: (print("\nStopping after in-flight requests ...",
                                                    file=sys.stderr), control.stop()))
    last_print = [0.0]

    def on_progress(p: Progress) -> None:
        now = time.time()
        if p.phase == "describing" and now - last_print[0] < 1.0:
            return
        last_print[0] = now
        line = (f"\r[{p.phase}] {p.done}/{p.total} done  ok={p.ok} dup={p.duplicates} "
                f"unreadable={p.unreadable} err={p.errors}  {p.rate_per_min:.0f}/min  "
                f"ETA {_fmt_eta(p.eta_seconds)}  ${p.cost_usd:.4f}   ")
        sys.stderr.write(line)
        sys.stderr.flush()

    def on_log(s: str) -> None:
        sys.stderr.write("\n" + s + "\n")

    engine = None
    if not cfg.dedupe_only:
        try:
            engine = build_engine(cfg)
        except Exception as e:  # noqa: BLE001
            print(f"Cannot start engine: {e}", file=sys.stderr)
            return 2
    runner = CatalogRunner(cfg, engine, control, on_progress, on_log)
    result = runner.run()
    sys.stderr.write("\n")
    print(f"{result.phase}: {result.ok} described, {result.duplicates} duplicates, "
          f"{result.unreadable} unreadable, {result.errors} errors, {result.pending} pending. "
          f"Tokens in/out {result.input_tokens}/{result.output_tokens}, est. ${result.cost_usd:.4f}.")
    print(f"CSV: {cfg.output_csv}\nJSON: {cfg.output_json}\nDB: {cfg.db_path}")
    if result.duplicates:
        print(f"Duplicates: {cfg.output_duplicates_csv}")
    return 0 if result.phase == "done" and not runner.fatal_error else 1


if __name__ == "__main__":
    sys.exit(main())
