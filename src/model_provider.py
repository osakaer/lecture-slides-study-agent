"""Local Ollama client for structured, multimodal generation."""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("OLLAMA_MODEL", "qwen3-vl:2b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "600"))


def _request_with_progress(request: urllib.request.Request) -> dict:
    started_at = time.monotonic()
    stop_heartbeat = threading.Event()

    def report_progress() -> None:
        while not stop_heartbeat.wait(30):
            elapsed = int(time.monotonic() - started_at)
            print(f"  Local model is still generating ({elapsed}s elapsed)...", flush=True)

    heartbeat = threading.Thread(target=report_progress, daemon=True)
    heartbeat.start()
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=0.2)


def ensure_model_ready() -> None:
    """Fail early with setup guidance if Ollama or the configured model is missing."""
    try:
        request = urllib.request.Request(f"{OLLAMA_HOST}/api/tags")
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "Ollama is not responding. Open the Ollama app and make sure it is running."
        ) from error

    installed_models = {
        item.get("name") or item.get("model")
        for item in payload.get("models", [])
        if isinstance(item, dict)
    }
    required_models = {MODEL}
    verifier_model = os.getenv("VERIFIER_MODEL", "").strip()
    if verifier_model:
        required_models.add(verifier_model)
    missing_models = sorted(required_models - installed_models)
    if missing_models:
        missing_list = ", ".join(missing_models)
        raise RuntimeError(
            f"Local model(s) not installed: {missing_list}. Open a terminal and run: "
            + " then ".join(f"ollama pull {name}" for name in missing_models)
        )


def generate_json(
    prompt: str,
    schema: dict,
    image_paths: list[Path] | None = None,
    model: str | None = None,
) -> str:
    """Generate schema-constrained JSON using the local Ollama service."""
    images = []
    for image_path in image_paths or []:
        if not image_path.exists():
            raise FileNotFoundError(f"Slide image not found: {image_path}")
        images.append(base64.b64encode(image_path.read_bytes()).decode("ascii"))

    message = {"role": "user", "content": prompt}
    if images:
        message["images"] = images
    payload = {
        "model": model or MODEL,
        "messages": [message],
        "format": schema,
        "options": {"temperature": 0},
        "stream": False,
    }
    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        result = _request_with_progress(request)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Ollama API returned HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(
            f"The local model did not finish within {OLLAMA_TIMEOUT_SECONDS} seconds. "
            "Try a smaller deck or fewer questions, and close other GPU-heavy apps."
        ) from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Ollama returned an invalid JSON response.") from error

    text = result.get("message", {}).get("content")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Ollama returned an empty response.")
    return text


def provider_info(model: str | None = None) -> dict:
    """Return local model metadata for logs and verification results."""
    return {"provider": "ollama", "model": model or MODEL}
