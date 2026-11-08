"""Internal worker that builds a study guide for a web job."""

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

from evidence import evidence_score


PROJECT_DIR = Path(__file__).resolve().parent.parent

if hasattr(sys.stdout, "reconfigure"):
    # The web server reads worker output as live job progress.
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
    if sum(capacities.values()) < target:
        raise SystemExit(
            f"This deck has only {sum(capacities.values())} usable slides for "
            f"question generation; reduce the question count below {target}."
        )

    allocation = {number: 0 for number in capacities}
    while sum(allocation.values()) < target:
        for section_number in allocation:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a study guide for one uploaded PDF.")
    parser.add_argument("pdf", help="Uploaded PDF path")
    parser.add_argument("run_dir", help="Temporary directory for this web job")
    parser.add_argument("--questions", type=int, default=12, choices=range(10, 16))
    args = parser.parse_args(argv)

    pdf_path = Path(args.pdf)
    if not pdf_path.is_absolute():
        pdf_path = PROJECT_DIR / pdf_path
    pdf_path = pdf_path.resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise SystemExit(f"PDF not found or not a PDF file: {pdf_path}")

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_DIR / run_dir
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir_arg = str(run_dir)

    run_step("Extract slides and render images", "src/extract.py", str(pdf_path), run_dir_arg)
    run_step("Create slide sections", "src/chunk.py", run_dir_arg)

    sections = json.loads((run_dir / "sections.json").read_text(encoding="utf-8"))
    slides = json.loads((run_dir / "extracted.json").read_text(encoding="utf-8"))
    allocation = allocate_questions(sections, slides, args.questions)

    for section in sections:
        number = section["section"]
        run_step(
            f"Summarize section {number}/{len(sections)}",
            "src/summarize.py",
            run_dir_arg,
            str(number),
        )

    completed = 0
    for section_number, count in allocation.items():
        for question_number in range(1, count + 1):
            completed += 1
            run_step(
                f"Generate question {completed}/{args.questions} "
                f"(section {section_number}, candidate {question_number})",
                "src/questions.py",
                run_dir_arg,
                str(section_number),
                str(question_number),
            )

    run_step("Collect question candidates", "src/collect_questions.py", run_dir_arg)
    run_step("Verify questions against cited slides", "src/verify.py", run_dir_arg)
    run_step("Repair unsupported questions", "src/repair_questions.py", run_dir_arg)
    run_step("Recheck repaired questions", "src/verify.py", run_dir_arg)
    run_step("Export the study guide", "src/export.py", run_dir_arg)
    print(f"\nStudy guide ready: {run_dir / 'study_guide.pdf'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
