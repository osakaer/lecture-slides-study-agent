"""Summarize one slide section with citations to its source slides."""

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from model_provider import MODEL, generate_json
from models import SectionSummary


def _next_invalid_path(path: Path) -> Path:
    candidate = path.with_name(f"{path.stem}.invalid{path.suffix}")
    version = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}.invalid_{version}{path.suffix}")
        version += 1
    return candidate


def _parse_summary(raw_text: str, valid_slides: set[int]) -> SectionSummary:
    summary = SectionSummary.model_validate_json(raw_text)
    for point in summary.key_points:
        citations = set(point.source_slides)
        invalid_citations = citations - valid_slides
        if not citations or invalid_citations:
            raise ValueError(
                f"Invalid citations {sorted(invalid_citations)}; allowed PDF slide "
                f"numbers are {sorted(valid_slides)}."
            )
    return summary


def _summary_schema(valid_slides: set[int]) -> dict:
    """Restrict generated citations to the PDF pages in this section."""
    schema = SectionSummary.model_json_schema()
    citation_items = schema["$defs"]["KeyPoint"]["properties"]["source_slides"]["items"]
    citation_items["enum"] = sorted(valid_slides)
    schema["$defs"]["KeyPoint"]["properties"]["source_slides"]["minItems"] = 1
    return schema


def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
    valid_slides = set(section["slide_numbers"])
    slide_lookup = {slide["slide"]: slide for slide in slides}
    raw_dir = run_dir / "raw_responses"
    summary_dir = run_dir / "section_summaries"
    raw_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"section_{section_number:02}.json"
    summary_path = summary_dir / f"section_{section_number:02}.json"

    if raw_path.exists():
        cached_text = raw_path.read_text(encoding="utf-8")
        try:
            summary = _parse_summary(cached_text, valid_slides)
        except (ValidationError, ValueError) as error:
            invalid_path = _next_invalid_path(raw_path)
            raw_path.replace(invalid_path)
            print(
                f"Cached summary is invalid ({error}); preserved at {invalid_path}. "
                "Generating a replacement.",
                flush=True,
            )
        else:
            summary_path.write_text(
                summary.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(summary.model_dump_json(indent=2, ensure_ascii=False))
            print(f"\nLoaded cached summary from {raw_path}")
            return

    allowed_slides = ", ".join(str(number) for number in sorted(valid_slides))
    prompt_parts = [
        "Create a concise study summary for this slide section. Include 4 to 8 key "
        "points. Each point must cite one or more PDF slide numbers from this exact "
        f"allowlist: {allowed_slides}. These are 1-based PDF page positions; ignore any "
        "different page or chapter numbers printed inside the slide artwork. Cite only "
        "slides that directly support the point. Use only the supplied slides; do not "
        "guess unclear details. Preserve technical mechanisms and identifying fields. "
        "Return concise JSON matching the required schema."
    ]
    image_paths = []
    for slide_number in section["slide_numbers"]:
        slide = slide_lookup[slide_number]
        image_path = Path(slide["image"])
        if not image_path.exists():
            raise SystemExit(f"Slide image not found: {image_path}")
        prompt_parts.append(f"Slide {slide_number} extracted text:\n{slide['text']}")
        image_paths.append(image_path)
    prompt_parts.append(
        "Attached slide images follow the same order as the slide text blocks above."
    )
    prompt = "\n\n".join(prompt_parts)

    for attempt in range(1, 3):
        attempt_prompt = prompt
        if attempt == 2:
            attempt_prompt += (
                "\n\nThe prior response was invalid. Return complete JSON, include 4 to 8 "
                f"key points, and cite only these PDF slide numbers: {allowed_slides}. "
                "Ignore numbers printed inside the slide artwork."
            )
        print(
            f"Summarizing section {section_number} with local model {MODEL} "
            f"(attempt {attempt}/2)...",
            flush=True,
        )
        raw_text = generate_json(
            attempt_prompt, _summary_schema(valid_slides), image_paths
        )
        try:
            summary = _parse_summary(raw_text, valid_slides)
        except (ValidationError, ValueError) as error:
            invalid_path = _next_invalid_path(raw_path)
            invalid_path.write_text(raw_text, encoding="utf-8")
            if attempt == 2:
                raise SystemExit(
                    f"Model returned an invalid summary twice: {error}. "
                    f"Responses preserved at {invalid_path}."
                ) from error
            print(f"Invalid summary preserved at {invalid_path}: {error}", flush=True)
            continue

        raw_path.write_text(raw_text, encoding="utf-8")
        summary_path.write_text(
            summary.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(summary.model_dump_json(indent=2, ensure_ascii=False))
        print(f"\nSaved summary to {summary_path}")
        print(f"Saved raw model response to {raw_path}")
        return


if __name__ == "__main__":
    main()
