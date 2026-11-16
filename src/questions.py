import json
import sys
from pathlib import Path
from pydantic import ValidationError
from model_provider import MODEL, generate_json
from models import ExamQuestion, ExamQuestionBatch
from evidence import evidence_score


def generate_deck_batch(run_dir: Path, allocation: dict[int, int]) -> None:
    sections = json.loads((run_dir / "sections.json").read_text(encoding="utf-8"))
    slides = json.loads((run_dir / "extracted.json").read_text(encoding="utf-8"))
    slide_lookup = {slide["slide"]: slide for slide in slides}
    section_lookup = {section["section"]: section for section in sections}
    assignments = []
    for section_number, count in allocation.items():
        section = section_lookup.get(section_number)
        if section is None:
            raise SystemExit(f"Invalid section number: {section_number}.")
        ranked_slide_numbers = [
            number for number in sorted(
                section["slide_numbers"],
                key=lambda number: evidence_score(slide_lookup[number]["text"]),
                reverse=True,
            ) if evidence_score(slide_lookup[number]["text"]) >= 0
        ]
        if count < 0 or count > len(ranked_slide_numbers):
            raise SystemExit(
                f"Section {section_number} has only {len(ranked_slide_numbers)} slides "
                "with usable evidence for question generation."
            )
        for candidate_number, slide_number in enumerate(ranked_slide_numbers[:count], start=1):
            assignments.append({
                "question_number": len(assignments) + 1,
                "source_section": section_number,
                "candidate_number": candidate_number,
                "source_slide": slide_number,
            })
    count = len(assignments)
    if count == 0:
        raise SystemExit("Question allocation is empty.")

    question_dir = run_dir / "questions"
    raw_dir = run_dir / "raw_responses"
    question_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"question_batch_{count:02}.json"

    prompt_parts = [
        f"Generate exactly {count} distinct exam-style questions in one batch. Each item's "
        "question_number, source_section, candidate_number, and source_slide must match its "
        "assignment block. Use only that item's evidence block; do not mix facts across blocks. "
        "Choose definition, why/how, compare, or application as appropriate. Do not add facts "
        "from memory, ask about unanswered exercises, or mention slide/page numbers in the "
        "question or answer. Keep each answer to at most 2 sentences and 45 words. "
        "Return only JSON matching the schema."
    ]
    image_paths = []
    assignment_lookup = {}
    for assignment in assignments:
        question_number = assignment["question_number"]
        section_number = assignment["source_section"]
        slide_number = assignment["source_slide"]
        slide = slide_lookup[slide_number]
        image_path = Path(slide["image"])
        if not image_path.exists():
            raise SystemExit(f"Slide image not found: {image_path}")
        assignment_lookup[question_number] = assignment
        prompt_parts.append(
            f"Assignment block {question_number}: question_number={question_number}, "
            f"source_section={section_number}, candidate_number={assignment['candidate_number']}, "
            f"source_slide={slide_number}. Use only this block for this question.\n"
            f"Extracted slide text:\n{slide['text']}"
        )
        image_paths.append(image_path)
    prompt_parts.append(
        "The attached images follow assignment-block order. Use each image only for the "
        "question assigned to its matching block."
    )
    prompt = "\n\n".join(prompt_parts)

    def parse_and_save(raw_text: str):
        batch = ExamQuestionBatch.model_validate_json(raw_text)
        if len(batch.questions) != count:
            raise ValueError(f"Expected {count} questions; received {len(batch.questions)}.")
        numbers = [item.question_number for item in batch.questions]
        if sorted(numbers) != list(range(1, count + 1)):
            raise ValueError("question_number values must appear exactly once from 1 to the batch size.")
        for item in batch.questions:
            assignment = assignment_lookup[item.question_number]
            if (
                item.source_section != assignment["source_section"]
                or item.candidate_number != assignment["candidate_number"]
                or item.source_slide != assignment["source_slide"]
            ):
                raise ValueError(
                    f"Question {item.question_number} must match its assigned section, "
                    f"candidate, and slide ({assignment['source_section']}, "
                    f"{assignment['candidate_number']}, {assignment['source_slide']})."
                )

        for item in batch.questions:
            suffix = "" if item.candidate_number == 1 else f"_q{item.candidate_number:02}"
            output_path = question_dir / f"section_{item.source_section:02}{suffix}.json"
            if output_path.exists():
                try:
                    existing = ExamQuestion.model_validate_json(
                        output_path.read_text(encoding="utf-8")
                    )
                    if existing.source_slide == item.source_slide:
                        continue
                except ValidationError:
                    pass
            output_path.write_text(
                item.model_dump_json(
                    indent=2,
                    exclude={"question_number", "source_section", "candidate_number"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return batch

    if raw_path.exists():
        try:
            batch = parse_and_save(raw_path.read_text(encoding="utf-8"))
            print(f"Loaded cached batch of {count} questions for this deck.")
            return
        except (ValidationError, ValueError) as error:
            invalid_path = raw_path.with_name(f"{raw_path.stem}.invalid{raw_path.suffix}")
            version = 2
            while invalid_path.exists():
                invalid_path = raw_path.with_name(
                    f"{raw_path.stem}.invalid_{version}{raw_path.suffix}"
                )
                version += 1
            raw_path.replace(invalid_path)
            print(f"Cached batch was invalid ({error}); preserved at {invalid_path}.")

    for attempt in range(1, 3):
        attempt_prompt = prompt
        if attempt == 2:
            attempt_prompt += (
                "\n\nThe prior response was invalid. Return a complete batch with exactly "
                f"{count} items, question_number values 1..{count}, and concise answers."
            )
        print(
            f"Generating all {count} questions in one local model call...",
            flush=True,
        )
        raw_text = generate_json(
            attempt_prompt,
            ExamQuestionBatch.model_json_schema(),
            image_paths,
        )
        try:
            batch = parse_and_save(raw_text)
            raw_path.write_text(raw_text, encoding="utf-8")
            for item in sorted(batch.questions, key=lambda value: value.question_number):
                print(item.model_dump_json(
                    indent=2,
                    exclude={"question_number", "source_section", "candidate_number"},
                    ensure_ascii=False,
                ))
            print(f"Saved question batch to {raw_path}")
            return
        except (ValidationError, ValueError) as error:
            invalid_path = raw_path.with_name(
                f"{raw_path.stem}.invalid_{attempt}{raw_path.suffix}"
            )
            invalid_path.write_text(raw_text, encoding="utf-8")
            if attempt == 2:
                raise SystemExit(
                    f"The local model returned an invalid question batch twice: {error}. "
                    f"Responses preserved at {invalid_path}."
                ) from error
            print(f"Invalid batch preserved at {invalid_path}: {error}", flush=True)

def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    if len(arguments) > 3 and arguments[2] == "--batch-all":
        raw_allocation = json.loads(arguments[3])
        generate_deck_batch(
            Path(arguments[1]),
            {int(section): int(count) for section, count in raw_allocation.items()},
        )
        return

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


    # Give each question one evidence-rich slide to avoid mixing details across slides.
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
        f"from slide {target_slide_number} with local model {MODEL}...",
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
