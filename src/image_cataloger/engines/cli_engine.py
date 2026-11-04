"""Optional engine: shell out to the Claude Code CLI (`claude -p`).

For people with a Claude subscription but no API key. The CLI reads the files
itself with its Read tool, so no image bytes are sent by this process. Slower
and less controllable than the API engine; kept because it is proven to work.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

from ..imaging import UnreadableImage, read_dimensions
from ..parsing import parse_tab_lines
from ..scanner import is_video_file
from .base import DescribeResult, RunControl

log = logging.getLogger(__name__)

CLI_MODELS = ["haiku", "sonnet", "opus"]

PROMPT_TEMPLATE = (
    "You are cataloging image files. Read every file listed below with the Read tool. "
    "Issue several Read calls in the same turn (5 at a time) to go faster.\n"
    "Then output ONLY a list, one line per file, in exactly this format:\n"
    "<path><TAB><one factual line, under 20 words, describing what the image shows: main "
    "subject, setting, legible text, and whether it is a screenshot/scan/graphic rather than a photo>\n"
    "Use the path exactly as given. If a file cannot be read or is corrupt, write 'UNREADABLE' "
    "as the summary. No preamble, no numbering, no blank lines, no commentary.\n\n"
    "Files:\n{files}"
)


def find_claude_cli() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                      os.path.expanduser("~/.local/bin/claude")):
        if os.path.exists(candidate):
            return candidate
    return None


class CliEngine:
    name = "cli"

    def __init__(self, root: str, model: str = "haiku", timeout: float = 600.0,
                 executable: str | None = None) -> None:
        self.root = root
        self.model = model
        self.timeout = timeout
        self.executable = executable or find_claude_cli()
        if not self.executable:
            raise FileNotFoundError("Claude Code CLI ('claude') not found on PATH")

    def describe(self, paths: list[str], control: RunControl) -> list[DescribeResult]:
        results: dict[str, DescribeResult] = {}
        readable: list[str] = []
        for p in paths:
            if is_video_file(p):
                results[p] = DescribeResult(p, status="error",
                                            error="videos need the Claude API engine (frame sampling)")
                continue
            try:
                read_dimensions(p)
                readable.append(p)
            except UnreadableImage as e:
                results[p] = DescribeResult(p, status="unreadable", error=str(e))
        if readable and not control.stopped:
            for r in self._describe_batch(readable, control):
                results[r.path] = r
        return [results.get(p) or DescribeResult(p, status="error", error="stopped") for p in paths]

    def _describe_batch(self, paths: list[str], control: RunControl) -> list[DescribeResult]:
        if not control.wait_if_paused():
            return [DescribeResult(p, status="error", error="stopped") for p in paths]
        try:
            text, cost = self._run(paths)
        except Exception as e:
            return [DescribeResult(p, status="error", error=f"{type(e).__name__}: {e}"[:300]) for p in paths]

        found = parse_tab_lines(text)
        by_base = {}
        for name, summary in found.items():
            by_base.setdefault(os.path.basename(name), []).append(summary)
        out: list[DescribeResult] = []
        missing: list[str] = []
        share = cost / max(1, len(paths))
        for p in paths:
            summary = found.get(p)
            if summary is None:
                cands = by_base.get(os.path.basename(p), [])
                summary = cands[0] if len(cands) == 1 else None
            if summary is None:
                missing.append(p)
            elif summary.upper().startswith("UNREADABLE"):
                out.append(DescribeResult(p, status="unreadable", error="CLI reported unreadable", cost_usd=share))
            else:
                out.append(DescribeResult(p, summary=summary, status="ok", cost_usd=share))
        if missing:
            if len(paths) > 1:
                log.info("CLI batch missed %d file(s); re-asking individually", len(missing))
                for p in missing:
                    out.extend(self._describe_batch([p], control))
            else:
                out.append(DescribeResult(paths[0], status="error", error="no summary in CLI output", cost_usd=share))
        return out

    def _run(self, paths: list[str]) -> tuple[str, float]:
        prompt = PROMPT_TEMPLATE.format(files="\n".join(paths))
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        cmd = [
            self.executable, "-p", "--model", self.model, "--allowedTools", "Read",
            "--add-dir", self.root, "--output-format", "json", prompt,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        data = json.loads(proc.stdout)
        return data.get("result", "") or "", float(data.get("total_cost_usd", 0) or 0)
