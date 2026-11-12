"""Run the complete study-guide pipeline on every PDF in eval/decks."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
DECK_DIR = PROJECT_DIR / "eval" / "decks"
RUN_ROOT = PROJECT_DIR / "data" / "runs" / "evaluation"
MANIFEST_PATH = PROJECT_DIR / "eval" / "run_manifest.json"


def write_manifest(entries: list[dict]) -> None:
    MANIFEST_PATH.write_text(
        json.dumps({"runs": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def safe_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "deck"


def main() -> int:
    decks = sorted(DECK_DIR.glob("*.pdf"))
    if not decks:
        raise SystemExit(f"Add 3 to 5 PDF files to {DECK_DIR} first.")
    if not 3 <= len(decks) <= 5:
        raise SystemExit(f"Expected 3 to 5 evaluation PDFs; found {len(decks)}.")

    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    used_slugs: set[str] = set()

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
        if run_dir.exists():
            shutil.rmtree(run_dir)

        print(f"\n=== Evaluating {deck.name} ===", flush=True)
        command = [
            sys.executable,
            str(PROJECT_DIR / "src" / "pipeline_worker.py"),
            str(deck.resolve()),
            str(run_dir),
            "--questions",
            "12",
        ]
        result = subprocess.run(command, cwd=PROJECT_DIR, check=False)
        entry = {
            "deck_name": deck.name,
            "run_dir": run_dir.relative_to(PROJECT_DIR).as_posix(),
            "return_code": result.returncode,
        }
        entries.append(entry)
        write_manifest(entries)
        if result.returncode:
            raise SystemExit(
                f"Pipeline failed for {deck.name} (exit code {result.returncode}); "
                f"temporary results are in {run_dir}."
            )

    print(f"\nCompleted {len(entries)} deck runs. Manifest: {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
