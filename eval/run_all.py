"""Run or resume the evaluation pipeline for every PDF in eval/decks."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))
sys.path.insert(0, str(PROJECT_DIR / "eval"))

from generate_baseline import PROMPT_VERSION
from model_provider import MODEL, OLLAMA_NUM_CTX, ensure_model_ready


DECK_DIR = PROJECT_DIR / "eval" / "decks"
RUN_ROOT = PROJECT_DIR / "data" / "runs" / "evaluation"
MANIFEST_PATH = PROJECT_DIR / "eval" / "run_manifest.json"
SLIDE_DPI = int(os.getenv("SLIDE_DPI", "110"))
VERIFIER_MODEL = os.getenv("VERIFIER_MODEL", "").strip() or MODEL


def write_manifest(entries: list[dict]) -> None:
    MANIFEST_PATH.write_text(
        json.dumps({"runs": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def safe_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "deck"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def current_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def code_fingerprint() -> str:
    digest = hashlib.sha256()
    sources = sorted((PROJECT_DIR / "src").glob("*.py"))
    sources.extend(
        path
        for path in (
            PROJECT_DIR / "eval" / "run_all.py",
            PROJECT_DIR / "eval" / "generate_baseline.py",
        )
        if path.is_file()
    )
    for source in sources:
        digest.update(source.relative_to(PROJECT_DIR).as_posix().encode("utf-8"))
        digest.update(source.read_bytes())
    return digest.hexdigest()


def run_settings(deck: Path, commit: str) -> dict:
    return {
        "pdf_sha256": file_sha256(deck),
        "generator_model": MODEL,
        "verifier_model": VERIFIER_MODEL,
        "questions": 12,
        "baseline_prompt_version": PROMPT_VERSION,
        "baseline_input_mode": "extracted_text_only",
        "num_ctx": OLLAMA_NUM_CTX,
        "slide_dpi": SLIDE_DPI,
        "commit": commit,
        "code_fingerprint": code_fingerprint(),
    }


def has_compatible_run(run_dir: Path, settings: dict) -> bool:
    info_path = run_dir / "run_info.json"
    if not info_path.is_file():
        return False
    try:
        previous = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return all(previous.get(key) == value for key, value in settings.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="delete existing evaluation run folders and regenerate from scratch",
    )
    args = parser.parse_args(argv)

    decks = sorted(DECK_DIR.glob("*.pdf"))
    if not decks:
        raise SystemExit(f"Add 3 to 5 PDF files to {DECK_DIR} first.")
    if not 3 <= len(decks) <= 5:
        raise SystemExit(f"Expected 3 to 5 evaluation PDFs; found {len(decks)}.")

    try:
        ensure_model_ready()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error

    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    used_slugs: set[str] = set()
    decks_to_run = []
    commit = current_commit()

    for deck in decks:
        base_slug = safe_slug(deck.stem)
        slug = base_slug
        suffix = 2
        while slug in used_slugs:
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        used_slugs.add(slug)

        run_dir = (RUN_ROOT / slug).resolve()
        if run_dir.parent != RUN_ROOT.resolve():
            raise SystemExit(f"Refusing to use an unsafe evaluation path: {run_dir}")
        settings = run_settings(deck, commit)
        compatible = has_compatible_run(run_dir, settings)
        if run_dir.exists() and (args.fresh or not compatible):
            reason = "--fresh was requested" if args.fresh else "run metadata does not match"
            print(f"Removing {run_dir} because {reason}.", flush=True)
            shutil.rmtree(run_dir)
        elif compatible:
            print(f"Resuming compatible run for {deck.name}.", flush=True)
        decks_to_run.append((deck, run_dir, settings))

    entries = [
        {
            "deck_name": deck.name,
            "run_dir": run_dir.relative_to(PROJECT_DIR).as_posix(),
            "status": "pending",
            "pipeline_status": "pending",
            "baseline_status": "pending",
            "settings": settings,
        }
        for deck, run_dir, settings in decks_to_run
    ]
    write_manifest(entries)
    failures = []

    for index, (deck, run_dir, settings) in enumerate(decks_to_run):
        print(f"\n=== Evaluating {deck.name} ===", flush=True)
        command = [
            sys.executable,
            str(PROJECT_DIR / "src" / "pipeline_worker.py"),
            str(deck.resolve()),
            str(run_dir),
            "--questions",
            "12",
        ]
        environment = os.environ.copy()
        environment["BASELINE_PROMPT_VERSION"] = PROMPT_VERSION
        try:
            result = subprocess.run(
                command, cwd=PROJECT_DIR, env=environment, check=False
            )
            return_code = result.returncode
            error = None
        except OSError as exception:
            return_code = 1
            error = str(exception)

        entries[index]["pipeline_status"] = "completed" if return_code == 0 else "failed"
        entries[index]["pipeline_return_code"] = return_code
        if error:
            entries[index]["pipeline_error"] = error
        if return_code == 0:
            baseline_command = [
                sys.executable,
                str(PROJECT_DIR / "eval" / "generate_baseline.py"),
                str(run_dir),
                "--questions",
                "12",
            ]
            print("--- Generate naive single-prompt baseline ---", flush=True)
            try:
                baseline_result = subprocess.run(
                    baseline_command,
                    cwd=PROJECT_DIR,
                    env=environment,
                    check=False,
                )
                baseline_return_code = baseline_result.returncode
                baseline_error = None
            except OSError as exception:
                baseline_return_code = 1
                baseline_error = str(exception)
            entries[index]["baseline_status"] = (
                "completed" if baseline_return_code == 0 else "failed"
            )
            entries[index]["baseline_return_code"] = baseline_return_code
            if baseline_error:
                entries[index]["baseline_error"] = baseline_error
        else:
            entries[index]["baseline_status"] = "skipped"
            entries[index]["baseline_return_code"] = None
        combined_return_code = return_code or entries[index]["baseline_return_code"] or 0
        entries[index]["return_code"] = combined_return_code
        entries[index]["status"] = "completed" if combined_return_code == 0 else "failed"
        write_manifest(entries)
        if combined_return_code:
            failures.append(deck.name)
            print(
                f"Evaluation failed for {deck.name} (pipeline exit {return_code}, "
                f"baseline exit {entries[index]['baseline_return_code']}); "
                f"partial results remain in {run_dir}.",
                flush=True,
            )

    if failures:
        print(
            f"\nEvaluation finished with {len(failures)} failed deck(s): "
            + ", ".join(failures)
            + f". Manifest: {MANIFEST_PATH}",
            flush=True,
        )
        return 1

    print(f"\nCompleted {len(entries)} deck runs. Manifest: {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
