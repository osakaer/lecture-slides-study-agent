"""Generate a one-prompt, text-only question baseline for one evaluation deck."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ValidationError

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

from model_provider import MODEL, OLLAMA_NUM_CTX, generate_json
from models import ExamQuestion


PROMPT_VERSION = "baseline-v1"


class BaselineQuestionSet(BaseModel):
    """Question list returned by the single-prompt baseline."""

    questions: list[ExamQuestion]


def text_sha256(slides: list[dict]) -> str:
    text = json.dumps(
        [{"slide": slide["slide"], "text": slide.get("text", "")} for slide in slides],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def next_invalid_path(path: Path) -> Path:
    candidate = path.with_name(f"{path.stem}.invalid{path.suffix}")
    version = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}.invalid_{version}{path.suffix}")
        version += 1
    return candidate


def baseline_schema(slide_numbers: list[int], question_count: int) -> dict:
    schema = BaselineQuestionSet.model_json_schema()
    schema["properties"]["questions"]["minItems"] = question_count
    schema["properties"]["questions"]["maxItems"] = question_count
    schema["$defs"]["ExamQuestion"]["properties"]["source_slide"]["enum"] = slide_numbers
    return schema


def validate_response(raw_text: str, slide_numbers: set[int], count: int) -> BaselineQuestionSet:
    result = BaselineQuestionSet.model_validate_json(raw_text)
    if len(result.questions) != count:
        raise ValueError(f"Expected {count} questions; received {len(result.questions)}.")
    if any(question.source_slide not in slide_numbers for question in result.questions):
        raise ValueError("A baseline question cited a slide outside the PDF.")
    normalized = [" ".join(question.question.casefold().split()) for question in result.questions]
    if len(set(normalized)) != count:
        raise ValueError("The baseline response contains duplicate questions.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Completed pipeline run directory")
    parser.add_argument("--questions", type=int, default=12)
    args = parser.parse_args(argv)
    if args.questions < 1:
        raise SystemExit("Question count must be at least 1.")

    run_dir = args.run_dir.resolve()
    extracted_path = run_dir / "extracted.json"
    if not extracted_path.is_file():
        raise SystemExit(f"Extracted slides not found: {extracted_path}")
    slides = json.loads(extracted_path.read_text(encoding="utf-8"))
    if not slides:
        raise SystemExit("No extracted slides are available for the baseline.")
    slide_numbers = {int(slide["slide"]) for slide in slides}
    baseline_dir = run_dir / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    questions_path = baseline_dir / "questions.json"
    raw_path = baseline_dir / "raw_response.json"
    info_path = baseline_dir / "run_info.json"

    pipeline_info = json.loads((run_dir / "run_info.json").read_text(encoding="utf-8"))
    run_info = {
        "method": "naive_single_prompt_text_only",
        "prompt_version": PROMPT_VERSION,
        "model": MODEL,
        "num_ctx": OLLAMA_NUM_CTX,
        "question_count": args.questions,
        "pdf_sha256": pipeline_info.get("pdf_sha256"),
        "extracted_text_sha256": text_sha256(slides),
        "commit": pipeline_info.get("commit", "unknown"),
        "code_fingerprint": pipeline_info.get("code_fingerprint", "unknown"),
    }
    if info_path.is_file() and questions_path.is_file():
        try:
            previous_info = json.loads(info_path.read_text(encoding="utf-8"))
            cached_raw = raw_path.read_text(encoding="utf-8")
            cached_questions = validate_response(cached_raw, slide_numbers, args.questions)
        except (OSError, json.JSONDecodeError, ValidationError, ValueError):
            pass
        else:
            if all(previous_info.get(key) == value for key, value in run_info.items()):
                print(f"Reusing compatible baseline with {len(cached_questions.questions)} questions.")
                return 0

    prompt_parts = [
        f"Generate exactly {args.questions} distinct exam-style questions from the complete "
        "lecture deck in one response. This is a naive single-prompt baseline: do not use "
        "section summaries, retrieval, question repair, or verification. Use only the slide "
        "text below. Each question must have a concise answer, a type, and the 1-based PDF "
        "slide number that directly supports its answer. Ignore numbers printed inside slide "
        "artwork when identifying PDF slide numbers. Do not add facts from memory, and do not "
        "mention slide numbers in the question or answer. Keep answers under 45 words. "
        "Return only JSON matching the schema."
    ]
    for slide in slides:
        prompt_parts.append(f"PDF slide {slide['slide']} text:\n{slide.get('text', '')}")
    prompt = "\n\n".join(prompt_parts)
    print(
        f"Generating {args.questions} baseline questions in one {MODEL} call "
        f"(prompt version {PROMPT_VERSION}; text-only input)...",
        flush=True,
    )
    try:
        raw_text = generate_json(
            prompt, baseline_schema(sorted(slide_numbers), args.questions)
        )
    except RuntimeError as error:
        detail = str(error)
        if "context" in detail.casefold():
            raise SystemExit(
                f"The complete-deck baseline did not fit the configured Ollama context "
                f"({OLLAMA_NUM_CTX}). It does not truncate slide text. Use a shorter "
                f"evaluation deck or adjust OLLAMA_NUM_CTX. Details: {detail}"
            ) from error
        raise SystemExit(f"Baseline generation failed: {detail}") from error
    raw_path.write_text(raw_text, encoding="utf-8")
    try:
        questions = validate_response(raw_text, slide_numbers, args.questions)
    except (ValidationError, ValueError) as error:
        invalid_path = next_invalid_path(raw_path)
        raw_path.replace(invalid_path)
        raise SystemExit(
            f"Baseline response was invalid: {error}. Raw output preserved at {invalid_path}."
        ) from error

    questions_path.write_text(
        json.dumps(
            {"question_count": len(questions.questions), "questions": [
                question.model_dump(mode="json") for question in questions.questions
            ]},
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    info_path.write_text(
        json.dumps(run_info, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Saved baseline questions to {questions_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
