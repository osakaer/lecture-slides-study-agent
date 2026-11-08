"""Gemini API client for structured, multimodal generation."""

import base64
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Gemini configuration
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low").lower()
if GEMINI_THINKING_LEVEL not in {"low", "medium", "high"}:
    raise ValueError("GEMINI_THINKING_LEVEL must be low, medium, or high")

MODEL = GEMINI_MODEL


# ── Gemini backend ─────────────────────────────────────────────────────────

_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        from google.genai import types

        _gemini_client = genai.Client(
            http_options=types.HttpOptions(
                timeout=120_000,
                # The Interactions endpoint has its own retry loop. Disable it so
                # the bounded retry policy below controls 429/503 backoff.
                retry_options=types.HttpRetryOptions(attempts=0),
            )
        )
    return _gemini_client


def _gemini_generate_json(
    prompt: str,
    schema: dict,
    image_paths: list[Path] | None = None,
) -> str:
    from google import genai

    client = _get_gemini_client()
    image_paths = image_paths or []

    input_parts: list[dict] = [{"type": "text", "text": prompt}]
    for image_path in image_paths:
        if not image_path.exists():
            raise FileNotFoundError(f"Slide image not found: {image_path}")
        image_data = base64.b64encode(image_path.read_bytes()).decode("utf-8")
        input_parts.append({
            "type": "image",
            "data": image_data,
            "mime_type": "image/png",
        })

    # Retry up to 3 times for transient 503 / rate-limit errors
    last_error = None
    for retry in range(3):
        try:
            response = client.interactions.create(
                model=GEMINI_MODEL,
                input=input_parts,
                generation_config={
                    "thinking_level": GEMINI_THINKING_LEVEL,
                    "temperature": 0,
                },
                timeout=120,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema_": schema,
                },
            )
            text = response.output_text
            if not text:
                raise RuntimeError("Gemini returned an empty response.")
            return text
        except Exception as error:
            last_error = error
            error_str = str(error).lower()
            if "timeout" in error_str or "timed out" in error_str:
                raise RuntimeError(
                    "Gemini did not respond within 120 seconds. The run stopped; "
                    "rerun with --resume after service is available."
                ) from error
            if any(
                marker in error_str
                for marker in ("503", "429", "high demand", "rate", "too many requests")
            ):
                if retry < 2:
                    import time
                    wait = 5 * (retry + 1)
                    print(
                        f"  Gemini rate-limited (attempt {retry + 1}/3). "
                        f"Retrying in {wait}s...",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(wait)
                continue
            raise RuntimeError(f"Gemini API error: {error}") from error

    raise RuntimeError(
        f"Gemini failed after 3 retries. Last error: {last_error}"
    ) from last_error


# ── Public interface ───────────────────────────────────────────────────────

def generate_json(
    prompt: str,
    schema: dict,
    image_paths: list[Path] | None = None,
) -> str:
    """Generate schema-constrained JSON with Gemini."""
    return _gemini_generate_json(prompt, schema, image_paths)


def provider_info() -> dict:
    """Return Gemini metadata for logging and verification results."""
    return {"provider": "gemini", "model": MODEL}
