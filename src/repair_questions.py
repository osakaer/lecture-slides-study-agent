"""Rewrite questions whose answers are not fully supported by their cited slides."""

import hashlib
import json
import sys
from pathlib import Path
from pydantic import ValidationError
from model_provider import MODEL, generate_json
from models import ExamQuestion, QuestionType

def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    run_dir = Path(arguments[1]) if len(arguments) > 1 else Path("data")
    question_path = run_dir / "questions/curated_set.json"
    verification_path = run_dir / "questions/verification.json"
    slides_path = run_dir / "extracted.json"
    repair_log_path = run_dir / "questions/repair_log.json"

    for required_path in (question_path, verification_path, slides_path):
        if not required_path.exists():
            raise SystemExit(f"Required pipeline file not found: {required_path}")

    question_set = json.loads(question_path.read_text(encoding="utf-8"))
    verification_set = json.loads(verification_path.read_text(encoding="utf-8"))
    slides = json.loads(slides_path.read_text(encoding="utf-8"))
    if len(question_set["questions"]) != len(verification_set["results"]):
        raise SystemExit("Question and verification counts differ; run verify.py first.")

    slide_lookup = {slide["slide"]: slide for slide in slides}
    raw_dir = run_dir / "raw_responses"
    raw_dir.mkdir(parents=True, exist_ok=True)
    repair_log = (
        json.loads(repair_log_path.read_text(encoding="utf-8"))
        if repair_log_path.exists()
        else {"repairs": []}
    )


    def write_json(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


    def next_invalid_path(path: Path) -> Path:
        candidate = path.with_name(f"{path.stem}.invalid{path.suffix}")
        version = 2
        while candidate.exists():
            candidate = path.with_name(f"{path.stem}.invalid_{version}{path.suffix}")
            version += 1
        return candidate


    def candidate_paths(section_number: int, candidate_number: int) -> tuple[Path, Path]:
        suffix = "" if candidate_number == 1 else f"_q{candidate_number:02}"
        raw_suffix = "" if candidate_number == 1 else f"_{candidate_number:02}"
        return (
            run_dir / "questions" / f"section_{section_number:02}{suffix}.json",
            raw_dir / f"question_{section_number:02}{raw_suffix}.json",
        )


    repairs_made = 0
    for index, (question, result) in enumerate(
        zip(question_set["questions"], verification_set["results"]),
        start=1,
    ):
        review = result["verification"]
        if review["supported"] == "yes":
            continue
        if (
            result.get("source_section") != question["source_section"]
            or result.get("question") != question["question"]
            or result.get("answer") != question["answer"]
            or result.get("source_slide") != question["source_slide"]
        ):
            raise SystemExit(
                f"Verification result {index} does not match curated question {index}; "
                "run verify.py again before repair."
            )

        section_number = int(question["source_section"])
        candidate_number = int(question.get("candidate_number", 1))
        prior_attempt = next(
            (
                item
                for item in reversed(repair_log["repairs"])
                if item["source_section"] == section_number
                and item["candidate_number"] == candidate_number
                and item["repaired_question"] == question["question"]
                and item["repaired_answer"] == question["answer"]
            ),
            None,
        )
        if prior_attempt is not None:
            print(
                f"Question {index}: the current wording was already repaired once; "
                "leaving it for manual review."
            )
            continue

        slide_number = int(question["source_slide"])
        slide = slide_lookup.get(slide_number)
        if slide is None:
            raise SystemExit(f"Cited slide {slide_number} is missing from {slides_path}.")
        image_path = Path(slide["image"])
        if not image_path.exists():
            raise SystemExit(f"Slide image not found: {image_path}")

        claims = review.get("claims", [])
        flagged_claims = [
            claim for claim in claims if claim.get("status") != "supported"
        ]
        prompt = (
            "Repair this exact study question because its cited-slide audit found unsupported "
            "or unclear claims. Keep the same learning objective and central concept. Make "
            "the smallest wording change that makes the question answerable from the cited "
            "slide; do not switch to an unrelated topic. Keep source_slide exactly "
            f"{slide_number}. Use only facts directly shown in the slide text or image. "
            "Remove unsupported details from the answer, and do not add outside knowledge. "
            "If the original question contains a false or unanswered premise, rewrite it so "
            "it asks only what the slide actually establishes. Keep the answer concise and "
            "do not mention slide/page numbers in the question or answer. Return one complete "
            "JSON object matching the schema.\n\n"
            f"Original question: {question['question']}\n"
            f"Original answer: {question['answer']}\n"
            f"Question type: {question['type']}\n"
            f"Citation audit: {review.get('explanation', '')}\n"
            f"Claims needing correction: {json.dumps(flagged_claims, ensure_ascii=False)}\n\n"
            f"Cited PDF slide {slide_number} extracted text:\n{slide['text']}"
        )

        old_question = question["question"]
        old_answer = question["answer"]
        repaired = None
        raw_text = ""
        source_fingerprint = hashlib.sha256(
            f"{question['question']}\n{question['answer']}".encode("utf-8")
        ).hexdigest()[:10]
        repair_raw_path = raw_dir / (
            f"repair_{section_number:02}_{candidate_number:02}_{source_fingerprint}.json"
        )

        for attempt in range(1, 3):
            attempt_prompt = prompt
            if attempt == 2:
                print(f"Question {index}: retrying repair with explicit revision requirement...", flush=True)
                attempt_prompt += (
                    "\n\nCRITICAL: Your previous output made no change or was invalid. "
                    "You MUST modify the wording to remove or rephrase the unsupported claims "
                    "identified in the audit. Do not return identical text. Keep the answer under 35 words."
                )
            print(
                f"Question {index}: regenerating the same question from slide {slide_number} "
                f"with {MODEL}...",
                flush=True,
            )
            try:
                raw_text = generate_json(
                    attempt_prompt,
                    ExamQuestion.model_json_schema(),
                    [image_path],
                )
            except RuntimeError as error:
                print(f"Model call failed during repair for question {index}: {error}")
                break

            try:
                candidate_repaired = ExamQuestion.model_validate_json(raw_text)
                if candidate_repaired.source_slide != slide_number:
                    raise ValueError(
                        f"source_slide must remain {slide_number}, not {candidate_repaired.source_slide}."
                    )
                if (
                    candidate_repaired.question.strip() == old_question.strip()
                    and candidate_repaired.answer.strip() == old_answer.strip()
                ):
                    raise ValueError(
                        "Repaired question and answer are identical to original; modification required."
                    )
                repaired = candidate_repaired
                break
            except (ValidationError, ValueError) as error:
                invalid_path = next_invalid_path(repair_raw_path)
                invalid_path.write_text(raw_text, encoding="utf-8")
                if attempt == 2:
                    print(
                        f"Question {index} could not be automatically modified ({error}). "
                        f"Retaining original for manual review. Responses preserved at {invalid_path}.",
                        flush=True,
                    )
                else:
                    print(f"Invalid or identical repair response preserved at {invalid_path}: {error}", flush=True)

        if repaired is None:
            # Record the failed repair so the question can be reviewed manually.
            repair_log["repairs"].append({
                "question_index": index,
                "source_section": section_number,
                "candidate_number": candidate_number,
                "source_slide": slide_number,
                "original_question": old_question,
                "original_answer": old_answer,
                "repaired_question": old_question,
                "repaired_answer": old_answer,
                "status": "unmodified_manual_review",
                "first_audit": review,
                "repair_response": str(repair_raw_path),
            })
            write_json(repair_log_path, repair_log)
            continue

        repair_raw_path.write_text(raw_text, encoding="utf-8")
        section_question_path, section_raw_path = candidate_paths(section_number, candidate_number)
        repaired_data = repaired.model_dump()

        question.update(repaired_data)
        question["include_slide_image"] = True
        write_json(question_path, question_set)
        write_json(section_question_path, repaired_data)
        section_raw_path.write_text(raw_text, encoding="utf-8")

        repair_log["repairs"].append({
            "question_index": index,
            "source_section": section_number,
            "candidate_number": candidate_number,
            "source_slide": slide_number,
            "original_question": old_question,
            "original_answer": old_answer,
            "repaired_question": repaired.question,
            "repaired_answer": repaired.answer,
            "status": "repaired",
            "first_audit": review,
            "repair_response": str(repair_raw_path),
        })
        write_json(repair_log_path, repair_log)
        repairs_made += 1
        print(
            f"Question {index} revised:\n{repaired.question}\nAnswer: {repaired.answer}\n"
            f"Saved to {section_question_path} and {question_path}.",
            flush=True,
        )

    if repairs_made == 0:
        print("No new repairs were needed; unsupported questions already had one repair attempt or all passed.")
    else:
        print(
            f"Revised {repairs_made} question(s). The pipeline should re-run verify.py "
            "to check each revision against its cited slide."
        )


if __name__ == '__main__':
    main()
