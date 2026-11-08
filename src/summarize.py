import json

import sys
from pathlib import Path

from model_provider import MODEL, generate_json
from models import KeyPoint, SectionSummary

def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

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
    print(f"Summarizing section {section_number} with Gemini {MODEL}...", flush=True)
    raw_text = generate_json(
        "\n\n".join(prompt_parts),
        SectionSummary.model_json_schema(),
        image_paths,
    )

    # Save the raw response before parsing
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
