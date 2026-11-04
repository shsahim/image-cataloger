"""Primary engine: the Claude Messages API with base64 image blocks.

Several images go into one request. The model must answer with a JSON array
containing exactly one string per image; if the count does not match (or the
response is unparseable), each image in the batch is re-asked on its own so a
bad batch never produces mismatched descriptions.
"""
from __future__ import annotations

import base64
import logging
import random

import anthropic

from ..imaging import PreparedImage, UnreadableImage, prepare_image
from ..parsing import parse_summaries
from ..pricing import cost_usd
from ..scanner import is_video_file
from ..video import VideoError, probe_video, sample_frames
from .base import DescribeResult, RunControl

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You catalog image files for a searchable index. For each image you are given, write "
    "exactly one factual line, under 20 words, describing what it shows: the main subject, "
    "the setting, any clearly legible text (quote it briefly), and the kind of image when it "
    "is not an ordinary photo (screenshot, scan, document, drawing, logo, graphic, meme, "
    "blank, corrupted). State only what is visible. Do not guess who people are, do not add "
    "opinions, and do not mention that you are describing an image. When given several frames "
    "sampled from one video, write one line describing the video as a whole."
)


class FatalEngineError(Exception):
    """An error that will not go away by retrying (bad key, no credit): stop the run."""


class ApiEngine:
    name = "api"

    def __init__(
        self,
        api_key: str | None,
        model: str,
        max_side: int = 1568,
        jpeg_quality: int = 85,
        effort: str = "low",
        timeout: float = 120.0,
        max_attempts: int = 8,
        client: anthropic.Anthropic | None = None,
        video_frames: int = 5,
    ) -> None:
        self.model = model
        self.max_side = max_side
        self.jpeg_quality = jpeg_quality
        self.effort = effort
        self.max_attempts = max_attempts
        self.video_frames = video_frames
        # Our own backoff loop handles 429/529, so keep the SDK's retries minimal.
        self.client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)

    # ---- public -----------------------------------------------------------
    def describe(self, paths: list[str], control: RunControl) -> list[DescribeResult]:
        prepared: list[tuple[str, PreparedImage]] = []
        results: dict[str, DescribeResult] = {}
        for p in paths:
            if is_video_file(p):
                if not control.stopped:
                    results[p] = self._describe_video(p, control)
                continue
            try:
                prepared.append((p, prepare_image(p, self.max_side, self.jpeg_quality)))
            except UnreadableImage as e:
                results[p] = DescribeResult(p, status="unreadable", error=str(e))
        if prepared and not control.stopped:
            for r in self._describe_prepared(prepared, control):
                results[r.path] = r
        return [results.get(p) or DescribeResult(p, status="error", error="stopped") for p in paths]

    # ---- internals --------------------------------------------------------
    def _describe_video(self, path: str, control: RunControl) -> DescribeResult:
        """One request per video: N evenly spaced frames, one line back."""
        try:
            info = probe_video(path)
            frames = sample_frames(path, self.video_frames, self.max_side, self.jpeg_quality,
                                   duration=info.duration)
        except VideoError as e:
            status = "error" if "not found" in str(e) or "timed out" in str(e) else "unreadable"
            return DescribeResult(path, status=status, error=str(e)[:300])
        secs = f"{info.duration:.0f}s" if info.duration else "unknown length"
        instruction = (
            f"The {len(frames)} image(s) above are frames sampled in order, evenly, from one video "
            f"({secs}). Describe what the video shows overall in one factual line under 20 words. "
            "Respond with ONLY a JSON array containing exactly 1 string, and nothing else."
        )
        items = [(path, f) for f in frames]
        try:
            text, usage = self._request(items, control, instruction=instruction,
                                        label=lambda i, n: f"Frame {i} of {n}:")
        except FatalEngineError:
            raise
        except Exception as e:  # noqa: BLE001
            return DescribeResult(path, status="error", error=_short(e))
        share = _split_usage(self.model, usage, 1)[0]
        if text is None:
            return DescribeResult(path, status="error", error="refused by safety classifier", **share)
        parsed = parse_summaries(text, 1)
        if parsed is None:
            return DescribeResult(path, status="error", error="unparseable response", **share)
        prefix = f"Video, {secs}: " if info.duration else "Video: "
        return DescribeResult(path, summary=prefix + parsed[0], status="ok", **share)

    def _describe_prepared(
        self, items: list[tuple[str, PreparedImage]], control: RunControl
    ) -> list[DescribeResult]:
        try:
            text, usage = self._request(items, control)
        except anthropic.RequestTooLargeError:
            if len(items) > 1:
                return self._one_by_one(items, control)
            return [DescribeResult(items[0][0], status="error", error="request too large")]
        except FatalEngineError:
            raise
        except Exception as e:  # retries exhausted, stopped, or non-retryable 4xx
            return [DescribeResult(p, status="error", error=_short(e)) for p, _ in items]

        summaries = parse_summaries(text, len(items)) if text is not None else None
        if summaries is None:
            if len(items) > 1:
                log.info("count mismatch on batch of %d; re-asking individually", len(items))
                return self._one_by_one(items, control)
            return [
                DescribeResult(
                    items[0][0],
                    status="error",
                    error=("refused by safety classifier" if text is None else "unparseable response"),
                    **_split_usage(self.model, usage, 1)[0],
                )
            ]
        shares = _split_usage(self.model, usage, len(items))
        return [
            DescribeResult(p, summary=s, status="ok", **share)
            for (p, _), s, share in zip(items, summaries, shares)
        ]

    def _one_by_one(
        self, items: list[tuple[str, PreparedImage]], control: RunControl
    ) -> list[DescribeResult]:
        out: list[DescribeResult] = []
        for item in items:
            if control.stopped:
                out.append(DescribeResult(item[0], status="error", error="stopped"))
                continue
            out.extend(self._describe_prepared([item], control))
        return out

    def _build_messages(self, items: list[tuple[str, PreparedImage]], instruction: str | None = None,
                        label=None) -> list[dict]:
        n = len(items)
        content: list[dict] = []
        for i, (_, img) in enumerate(items, start=1):
            content.append({"type": "text", "text": label(i, n) if label else f"Image {i} of {n}:"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.media_type,
                        "data": base64.standard_b64encode(img.data).decode("ascii"),
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": instruction or (
                    f"Describe each of the {n} image(s) above. Respond with ONLY a JSON array of "
                    f"exactly {n} strings, one per image in the same order, and nothing else."
                ),
            }
        )
        return [{"role": "user", "content": content}]

    def _request(
        self, items: list[tuple[str, PreparedImage]], control: RunControl,
        instruction: str | None = None, label=None,
    ) -> tuple[str | None, dict]:
        """Call the API with backoff. Returns (text, usage); text is None on a refusal."""
        messages = self._build_messages(items, instruction, label)
        attempt = 0
        while True:
            if not control.wait_if_paused():
                raise RuntimeError("stopped")
            attempt += 1
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=1024 + 128 * len(items),
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    output_config={"effort": self.effort},
                )
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                raise FatalEngineError(_short(e)) from e
            except anthropic.APIStatusError as e:
                if e.status_code == 402:
                    raise FatalEngineError(_short(e)) from e
                retryable = e.status_code in (408, 409, 429, 500, 502, 503, 529)
                if not retryable or attempt >= self.max_attempts:
                    raise
                delay = _backoff_delay(attempt, getattr(e, "response", None))
                log.warning("%s (attempt %d); retrying in %.1fs", _short(e), attempt, delay)
                if not control.sleep(delay):
                    raise RuntimeError("stopped") from e
                continue
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
                if attempt >= self.max_attempts:
                    raise
                delay = _backoff_delay(attempt, None)
                log.warning("%s (attempt %d); retrying in %.1fs", _short(e), attempt, delay)
                if not control.sleep(delay):
                    raise RuntimeError("stopped") from e
                continue

            usage = {
                "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
            }
            if response.stop_reason == "refusal":
                return None, usage
            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
            return text, usage


def _backoff_delay(attempt: int, response) -> float:
    retry_after = None
    try:
        if response is not None:
            retry_after = float(response.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        retry_after = None
    if retry_after:
        return min(120.0, retry_after + random.uniform(0, 1))
    return min(90.0, (2 ** attempt) + random.uniform(0, 1))


def _split_usage(model: str, usage: dict, n: int) -> list[dict]:
    """Attribute a batch's token usage evenly across its images (for the CSV/cost tally)."""
    total_in = int(usage.get("input_tokens", 0))
    total_out = int(usage.get("output_tokens", 0))
    shares = []
    for i in range(n):
        in_i = total_in // n + (1 if i < total_in % n else 0)
        out_i = total_out // n + (1 if i < total_out % n else 0)
        shares.append(
            {"input_tokens": in_i, "output_tokens": out_i, "cost_usd": cost_usd(model, in_i, out_i)}
        )
    return shares


def _short(e: Exception) -> str:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return f"{type(e).__name__}: {msg}"[:300]
