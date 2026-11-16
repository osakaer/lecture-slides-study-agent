"""Run the study-guide pipeline for one uploaded PDF."""

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from evidence import evidence_score
from model_provider import MODEL, OLLAMA_NUM_CTX, ensure_model_ready


PROJECT_DIR = Path(__file__).resolve().parent.parent

if hasattr(sys.stdout, "reconfigure"):
    # Streamlit reads worker output to show live progress.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def allocate_questions(sections: list[dict], slides: list[dict], target: int) -> dict[int, int]:
    slide_lookup = {slide["slide"]: slide for slide in slides}
    capacities = {
        section["section"]: sum(
            evidence_score(slide_lookup[number]["text"]) >= 0
            for number in section["slide_numbers"]
        )
        for section in sections
    }
    total = sum(capacities.values())
    if total < target:
        raise SystemExit(
            f"This deck has only {total} usable slides for "
            f"question generation; reduce the question count below {target}."
        )

    allocation = {number: 0 for number in capacities}
    usable = [number for number, capacity in capacities.items() if capacity > 0]

    if target <= len(usable):
        step = len(usable) / target
        for index in range(target):
            allocation[usable[int(index * step)]] += 1
        return allocation

    while sum(allocation.values()) < target:
        for section_number in usable:
            if allocation[section_number] < capacities[section_number]:
                allocation[section_number] += 1
                if sum(allocation.values()) == target:
                    break
    return allocation


def run_step(label: str, *args: str) -> None:
    print(f"\n=== {label} ===", flush=True)
    previous_directory = Path.cwd()
    os.chdir(PROJECT_DIR)
    try:
        stage = importlib.import_module(Path(args[0]).stem)
        stage.main(list(args[1:]))
    finally:
        os.chdir(previous_directory)


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
    for source in sources:
        digest.update(source.relative_to(PROJECT_DIR).as_posix().encode("utf-8"))
        digest.update(source.read_bytes())
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a study guide for one uploaded PDF.")
    parser.add_argument("pdf", help="Uploaded PDF path")
    parser.add_argument("run_dir", help="Temporary directory for this guide run")
    parser.add_argument("--questions", type=int, default=5, choices=(5, 10, 12, 15))
    args = parser.parse_args(argv)

    pdf_path = Path(args.pdf)
    if not pdf_path.is_absolute():
        pdf_path = PROJECT_DIR / pdf_path
    pdf_path = pdf_path.resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise SystemExit(f"PDF not found or not a PDF file: {pdf_path}")

    try:
        ensure_model_ready()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_DIR / run_dir
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    run_info = {
        "pdf_name": pdf_path.name,
        "pdf_sha256": file_sha256(pdf_path),
        "generator_model": MODEL,
        "verifier_model": os.getenv("VERIFIER_MODEL", "").strip() or MODEL,
        "questions": args.questions,
        "num_ctx": OLLAMA_NUM_CTX,
        "slide_dpi": int(os.getenv("SLIDE_DPI", "110")),
        "commit": current_commit(),
        "code_fingerprint": code_fingerprint(),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "run_info.json").write_text(
        json.dumps(run_info, indent=2) + "\n", encoding="utf-8"
    )
    run_dir_arg = str(run_dir)

    run_step("Extract slides and render images", "src/extract.py", str(pdf_path), run_dir_arg)
    run_step("Create slide sections", "src/chunk.py", run_dir_arg)

    sections = json.loads((run_dir / "sections.json").read_text(encoding="utf-8"))
    slides = json.loads((run_dir / "extracted.json").read_text(encoding="utf-8"))
    allocation = allocate_questions(sections, slides, args.questions)

    section_numbers = [section["section"] for section in sections]
    for section_number in section_numbers:
        run_step(
            f"Summarize section {section_number}/{len(sections)}",
            "src/summarize.py",
            run_dir_arg,
            str(section_number),
        )

    question_number = 0
    for section_number, count in allocation.items():
        for candidate_number in range(1, count + 1):
            question_number += 1
            run_step(
                f"Generate question {question_number}/{args.questions} from section {section_number}",
                "src/questions.py",
                run_dir_arg,
                str(section_number),
                str(candidate_number),
            )

    run_step("Collect question candidates", "src/collect_questions.py", run_dir_arg)
    run_step("Verify questions against cited slides", "src/verify.py", run_dir_arg)
    questions_dir = run_dir / "questions"
    for name in ("curated_set", "verification"):
        source = questions_dir / f"{name}.json"
        snapshot = questions_dir / f"{name}.pre_repair.json"
        if source.exists() and not snapshot.exists():
            shutil.copy2(source, snapshot)
    run_step("Repair unsupported questions", "src/repair_questions.py", run_dir_arg)
    run_step("Recheck repaired questions", "src/verify.py", run_dir_arg)
    run_step("Export the study guide", "src/export.py", run_dir_arg)
    print(f"\nStudy guide ready: {run_dir / 'study_guide.pdf'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
