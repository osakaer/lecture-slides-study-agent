import json
import sys
from pathlib import Path
from pydantic import ValidationError
from model_provider import MODEL, generate_json
from models import ExamQuestion, QuestionType
from evidence import evidence_score

def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    if len(arguments) > 1 and arguments[1].isdigit():
        run_dir = Path("data")
        section_number = int(arguments[1])
        question_number = int(arguments[2]) if len(arguments) > 2 else 1
    else:
        run_dir = Path(arguments[1]) if len(arguments) > 1 else Path("data")
        section_number = int(arguments[2]) if len(arguments) > 2 else 5
        question_number = int(arguments[3]) if len(arguments) > 3 else 1

    sections = json.loads((run_dir / "sections.json").read_text(encoding="utf-8"))
    slides = json.loads((run_dir / "extracted.json").read_text(encoding="utf-8"))
    if section_number < 1 or section_number > len(sections):
        raise SystemExit(f"Section number must be between 1 and {len(sections)}.")

    section = sections[section_number - 1]
    slide_lookup = {slide["slide"]: slide for slide in slides}


    # Give each generated question a distinct, evidence-rich slide. Restricting the
    # model to one slide also prevents it from mixing details across a six-slide batch.
    if question_number < 1:
        raise SystemExit("Question number must be 1 or greater.")

    ranked_slide_numbers = [
        slide_number
        for slide_number in sorted(
            section["slide_numbers"],
            key=lambda slide_number: evidence_score(slide_lookup[slide_number]["text"]),
            reverse=True,
        )
        if evidence_score(slide_lookup[slide_number]["text"]) >= 0
    ]
    if question_number > len(ranked_slide_numbers):
        raise SystemExit(
            f"Section {section_number} has only {len(ranked_slide_numbers)} slides "
            "with usable evidence; generate fewer questions for this section."
        )
    target_slide_number = ranked_slide_numbers[question_number - 1]
    target_slide = slide_lookup[target_slide_number]

    raw_dir = run_dir / "raw_responses"
    question_dir = run_dir / "questions"
    raw_dir.mkdir(parents=True, exist_ok=True)
    question_dir.mkdir(parents=True, exist_ok=True)

    if question_number == 1:
        raw_path = raw_dir / f"question_{section_number:02}.json"
        question_path = question_dir / f"section_{section_number:02}.json"
    else:
        raw_path = raw_dir / f"question_{section_number:02}_{question_number:02}.json"
        question_path = question_dir / f"section_{section_number:02}_q{question_number:02}.json"

    def save_question(raw_text: str) -> ExamQuestion:
        question = ExamQuestion.model_validate_json(raw_text)

        if question.source_slide != target_slide_number:
            raise ValueError(
                f"source_slide must be the assigned evidence slide {target_slide_number}, "
                f"not {question.source_slide}."
            )

        question_path.write_text(
            question.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return question


    def next_invalid_path(path: Path) -> Path:
        candidate = path.with_name(f"{path.stem}.invalid{path.suffix}")
        version = 2
        while candidate.exists():
            candidate = path.with_name(
                f"{path.stem}.invalid_{version}{path.suffix}"
            )
            version += 1
        return candidate


    if raw_path.exists():
        cached_text = raw_path.read_text(encoding="utf-8")
        try:
            question = save_question(cached_text)
        except (ValidationError, ValueError) as error:
            invalid_path = next_invalid_path(raw_path)
            raw_path.replace(invalid_path)
            print(
                f"Cached response is invalid ({error}). Preserved at {invalid_path}; "
                "generating a replacement."
            )
        else:
            print(question.model_dump_json(indent=2, ensure_ascii=False))
            print(f"\nLoaded cached response; saved question to {question_path}")
            return

    previous_questions = []
    candidate_paths = [question_dir / f"section_{section_number:02}.json"]
    candidate_paths.extend(sorted(question_dir.glob(f"section_{section_number:02}_q*.json")))
    for candidate_path in candidate_paths:
        if candidate_path == question_path or not candidate_path.exists():
            continue
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        previous_questions.append(
            f"Question: {candidate['question']}\nAnswer: {candidate['answer']}"
        )

    prompt_parts = [
            "Create exactly one non-trivial exam-style question. Choose the question "
            "type that best fits the evidence (definition, why/how, compare, or application); "
            "Use only the single supplied evidence slide. Base both question and answer "
            "entirely on that slide; do not add facts from memory or other slides. "
            "Do not turn an unanswered exercise, rhetorical question, or placeholder on "
            "the slide into a factual question; use only answers explicitly provided. "
            f"Set the source_slide metadata field to exactly {target_slide_number}. "
            "Do not mention slide or page numbers in the question or answer. Ignore printed "
            "chapter, lecture, or figure numbers inside the slide. The cited slide must "
            "directly support the answer. Use the slide image "
            "for diagrams when useful. Do not guess unreadable details or discuss information "
            "missing from the slide. Keep the answer to at most 2 sentences and 45 words. "
            "Do not repeat any earlier question "
            "from this section. Return JSON matching the schema."
        ]
    if previous_questions:
        prompt_parts.append(
            "Earlier questions from this section; create a different one:\n"
            + "\n\n".join(previous_questions)
        )
    image_path = Path(target_slide["image"])
    if not image_path.exists():
        raise SystemExit(f"Slide image not found: {image_path}")
    prompt_parts.append(
        f"Evidence slide: PDF slide {target_slide_number}.\n"
        f"Extracted text:\n{target_slide['text']}"
    )
    image_paths = [image_path]
    print(
        f"Generating question {question_number} for section {section_number} "
        f"from slide {target_slide_number} with Gemini {MODEL}...",
        flush=True,
    )
    generation_prompt = "\n\n".join(prompt_parts)
    question = None
    raw_text = ""
    for attempt in range(1, 3):
        if attempt == 2:
            print("First response was invalid; retrying once with a shorter-answer prompt.", flush=True)
            generation_prompt += (
                "\n\nYour previous output was invalid JSON or incomplete. Return only one "
                "complete JSON object matching the schema. Keep the answer under 35 words. "
                "Do not repeat phrases, add commentary, or end mid-sentence."
            )

        raw_text = generate_json(
            generation_prompt,
            ExamQuestion.model_json_schema(),
            image_paths,
        )
        try:
            question = save_question(raw_text)
            break
        except (ValidationError, ValueError) as error:
            invalid_path = next_invalid_path(raw_path)
            invalid_path.write_text(raw_text, encoding="utf-8")
            if attempt == 2:
                raise SystemExit(
                    f"Model returned invalid questions twice for section {section_number}: "
                    f"{error}. Responses preserved at {invalid_path} and the prior invalid file."
                ) from error
            print(
                f"Invalid response preserved at {invalid_path}: {error}",
                flush=True,
            )

    assert question is not None

    raw_path.write_text(raw_text, encoding="utf-8")

    print(question.model_dump_json(indent=2, ensure_ascii=False))
    print(f"\nSaved raw response to {raw_path}")
    print(f"Saved question to {question_path}")


if __name__ == '__main__':
    main()
