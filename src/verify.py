import hashlib
import json
import sys
from pathlib import Path
from pydantic import ValidationError
from model_provider import MODEL, generate_json, provider_info
from models import (
    ClaimAssessment,
    ClaimReview,
    ClaimReviewBatch,
)

def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv if argv is None else [sys.argv[0], *argv]
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    run_dir = Path(arguments[1]) if len(arguments) > 1 else Path("data")
    questions_path = run_dir / "questions/curated_set.json"
    slides_path = run_dir / "extracted.json"
    raw_dir = run_dir / "raw_responses"
    output_path = run_dir / "questions/verification.json"

    if not questions_path.exists():
        raise SystemExit(f"Question set not found: {questions_path}")
    if not slides_path.exists():
        raise SystemExit(f"Extracted slides not found: {slides_path}")

    question_set = json.loads(questions_path.read_text(encoding="utf-8"))
    slides = json.loads(slides_path.read_text(encoding="utf-8"))
    slide_lookup = {slide["slide"]: slide for slide in slides}
    raw_dir.mkdir(parents=True, exist_ok=True)

    results = []
    previous_results = []
    previous_provider = None
    if output_path.exists():
        previous_output = json.loads(output_path.read_text(encoding="utf-8"))
        previous_provider = previous_output.get("verifier")
        previous_results = previous_output.get("results", [])
    else:
        previous_results = []


    def save_results() -> None:
        counts = {status: 0 for status in ("yes", "partial", "no")}
        saved_results = [result for result in results if result is not None]
        for result in saved_results:
            counts[result["verification"]["supported"]] += 1

        output = {
            "verifier": provider_info(),
            "question_count": len(question_set["questions"]),
            "counts": counts,
            "results": saved_results,
        }
        output_path.write_text(
            json.dumps(output, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


    def aggregate_status(claims: list[ClaimAssessment]) -> str:
        statuses = {claim.status for claim in claims}
        if statuses == {"supported"}:
            return "yes"
        if "supported" in statuses or "unclear" in statuses:
            return "partial"
        return "no"


    def next_invalid_path(path: Path) -> Path:
        candidate = path.with_name(f"{path.stem}.invalid{path.suffix}")
        version = 2
        while candidate.exists():
            candidate = path.with_name(f"{path.stem}.invalid_{version}{path.suffix}")
            version += 1
        return candidate


    active_provider = provider_info()
    questions = question_set["questions"]
    results = [None] * len(questions)
    pending = []

    for index, question in enumerate(questions, start=1):
        slide_number = question["source_slide"]
        slide = slide_lookup.get(slide_number)
        if slide is None:
            raise SystemExit(
                f"Question {index} cites slide {slide_number}, which is not in {slides_path}."
            )
        image_path = Path(slide["image"])
        if not image_path.exists():
            raise SystemExit(f"Slide image not found: {image_path}")
        image_bytes = image_path.read_bytes()
        fingerprint_input = json.dumps(
            {
                "source_section": question.get("source_section"),
                "question": question["question"],
                "answer": question["answer"],
                "type": question["type"],
                "source_slide": slide_number,
                "slide_text": slide["text"],
                "verification_hint": question.get("verification_hint"),
                "provider": active_provider["provider"],
                "model": MODEL,
                "claim_review_version": 3,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8") + image_bytes
        fingerprint = hashlib.sha256(fingerprint_input).hexdigest()
        raw_path = raw_dir / f"verification_{index:02}_{fingerprint[:12]}.json"
        previous = next(
            (
                item for item in previous_results
                if item.get("source_section") == question.get("source_section")
                and item.get("question") == question["question"]
                and item.get("answer") == question["answer"]
                and item.get("source_slide") == slide_number
                and item.get("input_fingerprint") == fingerprint
            ),
            None,
        ) if previous_provider == active_provider else None

        verification = None
        review = None
        if previous is not None:
            verification = previous["verification"]
            print(f"Question {index}: reused matching saved result ({verification['supported']}).")
        elif raw_path.exists():
            raw_text = raw_path.read_text(encoding="utf-8")
            try:
                review = ClaimReview.model_validate_json(raw_text)
                verification = {
                    "supported": aggregate_status(review.claims),
                    "explanation": review.explanation,
                    "claims": [claim.model_dump() for claim in review.claims],
                }
                print(f"Question {index}: loaded cached result ({verification['supported']}).")
            except ValidationError:
                invalid_path = next_invalid_path(raw_path)
                invalid_path.write_text(raw_text, encoding="utf-8")
                raw_path.unlink()
                print(f"Question {index}: moved incomplete cached output to {invalid_path}.")

        entry = {
            "index": index,
            "question": question,
            "slide": slide,
            "fingerprint": fingerprint,
            "raw_path": raw_path,
        }
        if verification is None:
            pending.append(entry)
        else:
            results[index - 1] = {
                "source_section": question.get("source_section"),
                "question": question["question"],
                "answer": question["answer"],
                "type": question["type"],
                "source_slide": slide_number,
                "input_fingerprint": fingerprint,
                "verification": verification,
            }

    if pending:
        slide_numbers = list(dict.fromkeys(item["question"]["source_slide"] for item in pending))
        image_paths = []
        for slide_number in slide_numbers:
            image_path = Path(slide_lookup[slide_number]["image"])
            image_paths.append(image_path)

        prompt_parts = [
            "Audit each numbered question and answer independently against only its cited slide. "
            "For every answer, split each directly asserted factual statement into the smallest "
            "independently checkable claims. Do not invent, synthesize, or extrapolate claims. "
            "Include factual premises in the question that its answer depends on. Use only the "
            "text and matching image for that question; do not use outside knowledge or evidence "
            "from another question's slide. Use supported only when directly supported, "
            "contradicted when the slide conflicts, unsupported when absent, and unclear when "
            "ambiguous. For diagrams, cite visible labels or relationships. Do not treat an "
            "unanswered exercise as evidence. Keep claims, evidence, and explanations concise. "
            "Return exactly one result for every question_number provided, with no duplicates."
        ]
        for item in pending:
            question = item["question"]
            slide = item["slide"]
            prompt_parts.append(
                f"Question number: {item['index']}\n"
                f"Cited PDF slide: {question['source_slide']}\n"
                f"Question: {question['question']}\n"
                f"Answer: {question['answer']}\n"
                f"Extracted slide text:\n{slide['text']}"
                + (f"\nReview focus: {question['verification_hint']}"
                   if question.get("verification_hint") else "")
            )
        prompt_parts.append(
            "Attached slide images are ordered by these PDF slide numbers: "
            + ", ".join(str(number) for number in slide_numbers)
            + ". Use the image with the same cited slide number as each question."
        )
        prompt = "\n\n".join(prompt_parts)
        batch_key = hashlib.sha256(
            "|".join(item["fingerprint"] for item in pending).encode("ascii")
        ).hexdigest()[:16]
        batch_path = raw_dir / f"verification_batch_{batch_key}.json"

        def parse_batch(raw_text: str) -> ClaimReviewBatch:
            batch = ClaimReviewBatch.model_validate_json(raw_text)
            expected = {item["index"] for item in pending}
            found = [item.question_number for item in batch.results]
            if set(found) != expected or len(found) != len(expected):
                raise ValueError("Verification batch must return each pending question number exactly once.")
            return batch

        batch = None
        raw_text = ""
        if batch_path.exists():
            raw_text = batch_path.read_text(encoding="utf-8")
            try:
                batch = parse_batch(raw_text)
                print(f"Loaded cached verification batch for {len(pending)} questions.")
            except (ValidationError, ValueError) as error:
                invalid_path = next_invalid_path(batch_path)
                invalid_path.write_text(raw_text, encoding="utf-8")
                batch_path.unlink()
                print(f"Moved invalid verification batch to {invalid_path}: {error}")

        if batch is None:
            for attempt in range(1, 3):
                attempt_prompt = prompt
                if attempt == 2:
                    attempt_prompt += (
                        "\n\nThe prior batch was incomplete or invalid. Return a complete JSON "
                        "object containing each requested question_number exactly once."
                    )
                print(
                    f"Checking {len(pending)} questions against their cited slides in one Gemini call...",
                    flush=True,
                )
                try:
                    raw_text = generate_json(
                        attempt_prompt,
                        ClaimReviewBatch.model_json_schema(),
                        image_paths,
                    )
                except RuntimeError as error:
                    raise SystemExit(f"Model call failed while verifying question batch: {error}") from error
                try:
                    batch = parse_batch(raw_text)
                    batch_path.write_text(raw_text, encoding="utf-8")
                    break
                except (ValidationError, ValueError) as error:
                    invalid_path = batch_path.with_name(
                        f"{batch_path.stem}.invalid_{attempt}{batch_path.suffix}"
                    )
                    invalid_path.write_text(raw_text, encoding="utf-8")
                    if attempt == 2:
                        raise SystemExit(
                            f"Gemini returned an invalid verification batch twice; "
                            f"responses preserved at {invalid_path}: {error}"
                        ) from error
                    print(f"Invalid verification batch preserved at {invalid_path}: {error}")

        review_by_number = {item.question_number: item for item in batch.results}
        for entry in pending:
            index = entry["index"]
            question = entry["question"]
            slide_number = question["source_slide"]
            review = review_by_number[index]
            entry["raw_path"].write_text(
                ClaimReview(claims=review.claims, explanation=review.explanation).model_dump_json(
                    indent=2, ensure_ascii=False
                ),
                encoding="utf-8",
            )
            verification = {
                "supported": aggregate_status(review.claims),
                "explanation": review.explanation,
                "claims": [claim.model_dump() for claim in review.claims],
            }
            results[index - 1] = {
                "source_section": question.get("source_section"),
                "question": question["question"],
                "answer": question["answer"],
                "type": question["type"],
                "source_slide": slide_number,
                "input_fingerprint": entry["fingerprint"],
                "verification": verification,
            }
            print(f"Question {index}: {verification['supported']} — {verification['explanation']}")
            save_results()

    save_results()
    saved_output = json.loads(output_path.read_text(encoding="utf-8"))
    print(f"\nSaved verification results to {output_path}")
    print(f"Counts: {saved_output['counts']}")


if __name__ == '__main__':
    main()
