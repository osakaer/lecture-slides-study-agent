import json

import sys
from pathlib import Path
from pydantic import ValidationError

from model_provider import MODEL, generate_json
from models import KeyPoint, SectionSummary, SectionSummaryBatch


def summarize_batch(run_dir: Path, section_numbers: list[int]) -> None:
    sections = json.loads((run_dir / "sections.json").read_text(encoding="utf-8"))
    slides = json.loads((run_dir / "extracted.json").read_text(encoding="utf-8"))
    section_lookup = {section["section"]: section for section in sections}
    slide_lookup = {slide["slide"]: slide for slide in slides}
    raw_dir = run_dir / "raw_responses"
    summary_dir = run_dir / "section_summaries"
    raw_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)

    pending = []
    for number in section_numbers:
        if number not in section_lookup:
            raise SystemExit(f"Invalid section number: {number}")
        raw_path = raw_dir / f"section_{number:02}.json"
        summary_path = summary_dir / f"section_{number:02}.json"
        if raw_path.exists():
            summary = SectionSummary.model_validate_json(raw_path.read_text(encoding="utf-8"))
            summary_path.write_text(summary.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            pending.append(number)

    if not pending:
        print(f"Loaded cached summaries for sections {section_numbers[0]}–{section_numbers[-1]}.")
        return

    prompt_parts = [
        "Create one concise study summary for each numbered section. Keep facts and citations "
        "inside their own section; do not mix information between sections. Include 4 to 8 key "
        "points for each section, with the exact PDF slide numbers supporting each point. Use "
        "the extracted text and matching slide images, and do not guess unclear details. Return "
        "exactly one summary per requested section, as JSON matching the schema."
    ]
    image_paths = []
    image_slide_numbers = []
    for number in pending:
        section = section_lookup[number]
        prompt_parts.append(f"\nSECTION {number}: summarize only these slides.")
        for slide_number in section["slide_numbers"]:
            slide = slide_lookup[slide_number]
            image_path = Path(slide["image"])
            if not image_path.exists():
                raise SystemExit(f"Slide image not found: {image_path}")
            prompt_parts.append(
                f"Section {number}, PDF slide {slide_number} extracted text:\n{slide['text']}"
            )
            image_paths.append(image_path)
            image_slide_numbers.append((number, slide_number))
    prompt_parts.append(
        "Attached images follow this section and slide order: "
        + ", ".join(f"section {section}, slide {slide}" for section, slide in image_slide_numbers)
        + ". Use each image only for its listed section and slide."
    )
    prompt = "\n\n".join(prompt_parts)
    raw_batch_path = raw_dir / f"summary_batch_{pending[0]:02}_{pending[-1]:02}.json"

    def parse_batch(raw_text: str) -> SectionSummaryBatch:
        batch = SectionSummaryBatch.model_validate_json(raw_text)
        received = [item.section_number for item in batch.sections]
        if sorted(received) != sorted(pending) or len(received) != len(pending):
            raise ValueError("The response must contain each requested section exactly once.")
        for item in batch.sections:
            valid_slides = set(section_lookup[item.section_number]["slide_numbers"])
            for point in item.key_points:
                if not point.source_slides or not set(point.source_slides) <= valid_slides:
                    raise ValueError(
                        f"Section {item.section_number} contains a missing or out-of-section slide citation."
                    )
        return batch

    for attempt in range(1, 3):
        attempt_prompt = prompt
        if attempt == 2:
            attempt_prompt += (
                "\n\nThe prior response was invalid. Return all requested sections exactly once, "
                "and cite only slide numbers belonging to each section."
            )
        print(
            f"Summarizing {len(pending)} sections in one local model {MODEL} call...",
            flush=True,
        )
        raw_text = generate_json(
            attempt_prompt,
            SectionSummaryBatch.model_json_schema(),
            image_paths,
        )
        try:
            batch = parse_batch(raw_text)
            raw_batch_path.write_text(raw_text, encoding="utf-8")
            for item in batch.sections:
                summary = SectionSummary(
                    section_title=item.section_title,
                    key_points=item.key_points,
                )
                raw_path = raw_dir / f"section_{item.section_number:02}.json"
                summary_path = summary_dir / f"section_{item.section_number:02}.json"
                summary_json = summary.model_dump_json(indent=2, ensure_ascii=False)
                raw_path.write_text(summary_json, encoding="utf-8")
                summary_path.write_text(summary_json, encoding="utf-8")
            print(f"Saved batched summaries to {raw_batch_path}")
            return
        except (ValidationError, ValueError) as error:
            invalid_path = raw_dir / f"{raw_batch_path.stem}.invalid_{attempt}.json"
            invalid_path.write_text(raw_text, encoding="utf-8")
            if attempt == 2:
                raise SystemExit(
                    f"The local model returned an invalid summary batch twice: {error}. "
                    f"Responses preserved at {invalid_path}."
                ) from error
            print(f"Invalid summary batch preserved at {invalid_path}: {error}", flush=True)

def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    if len(arguments) > 4 and arguments[2] == "--batch":
        summarize_batch(
            Path(arguments[1]),
            list(range(int(arguments[3]), int(arguments[4]) + 1)),
        )
        return

    if len(arguments) > 1 and arguments[1].isdigit():
        run_dir = Path("data")
        section_number = int(arguments[1])
    else:
        run_dir = Path(arguments[1]) if len(arguments) > 1 else Path("data")
        section_number = int(arguments[2]) if len(arguments) > 2 else 3

    sections = json.loads((run_dir / "sections.json").read_text(encoding="utf-8"))
    slides = json.loads((run_dir / "extracted.json").read_text(encoding="utf-8"))

    if section_number < 1 or section_number > len(sections):
        raise SystemExit(f"Invalid section number: {section_number}")

    section = sections[section_number - 1]
    slide_lookup = {slide["slide"]: slide for slide in slides}

    raw_dir = run_dir / "raw_responses"
    summary_dir = run_dir / "section_summaries"
    raw_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)

    raw_path = raw_dir / f"section_{section_number:02}.json"
    summary_path = summary_dir / f"section_{section_number:02}.json"

    if raw_path.exists():
        raw_text = raw_path.read_text(encoding="utf-8")
        summary = SectionSummary.model_validate_json(raw_text)
        summary_path.write_text(
            summary.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(summary.model_dump_json(indent=2, ensure_ascii=False))
        print(f"\nSaved summary to {summary_path}")
        return

    prompt_parts = [
        "Create a concise study summary for this slide section. Use the extracted text "
        "and the slide images. Include 4 to 8 key points. Each point must list the slide "
        "numbers that support it. Use only the supplied slides; do not guess details that "
        "are unclear. Preserve technical mechanisms and identifying fields shown on the "
        "slides. Return concise JSON matching the required schema."
    ]
    image_paths = []

    for slide_number in section["slide_numbers"]:
        slide = slide_lookup[slide_number]
        image_path = Path(slide["image"])
        if not image_path.exists():
            raise SystemExit(f"Slide image not found: {image_path}")

        prompt_parts.append(
            f"Slide {slide_number} extracted text:\n{slide['text']}"
        )
        image_paths.append(image_path)

    prompt_parts.append("The attached slide images are in the same order as the slide text blocks above.")
    print(f"Summarizing section {section_number} with local model {MODEL}...", flush=True)
    raw_text = generate_json(
        "\n\n".join(prompt_parts),
        SectionSummary.model_json_schema(),
        image_paths,
    )

    # Save the raw response before parsing it.
    raw_path.write_text(raw_text, encoding="utf-8")

    summary = SectionSummary.model_validate_json(raw_text)
    summary_path.write_text(
        summary.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(summary.model_dump_json(indent=2))
    print(f"\nSaved summary to {summary_path}")
    print(f"Saved raw model response to {raw_path}")


if __name__ == '__main__':
    main()
