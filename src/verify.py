import hashlib
import json
import sys
from pathlib import Path
from pydantic import ValidationError
from model_provider import MODEL, generate_json, provider_info
from models import ClaimAssessment, ClaimReview, ClaimSource, ClaimStatus

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
        for result in results:
            counts[result["verification"]["supported"]] += 1

        output = {
            "verifier": provider_info(),
            "question_count": len(results),
            "counts": counts,
            "results": results,
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

    for index, question in enumerate(question_set["questions"], start=1):
        slide_number = question["source_slide"]
        slide = slide_lookup.get(slide_number)
        if slide is None:
            raise SystemExit(
                f"Question {index} cites slide {slide_number}, which is not in {slides_path}."
            )

        # Always include the rendered slide: diagrams may contain answer evidence that OCR misses.
        include_slide_image = True
        image_bytes = b""
        if include_slide_image:
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
                "include_slide_image": include_slide_image,
                "verification_hint": question.get("verification_hint"),
                "provider": active_provider["provider"],
                "model": MODEL,
                "claim_review_version": 2,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8") + image_bytes
        fingerprint = hashlib.sha256(fingerprint_input).hexdigest()
        raw_path = raw_dir / f"verification_{index:02}_{fingerprint[:12]}.json"

        previous = next(
            (
                item
                for item in previous_results
                if item.get("source_section") == question.get("source_section")
                and item.get("question") == question["question"]
                and item.get("answer") == question["answer"]
                and item.get("source_slide") == slide_number
                and item.get("input_fingerprint") == fingerprint
            ),
            None,
        ) if previous_provider == active_provider else None

        verification = None
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
                invalid_path = raw_path.with_name(f"{raw_path.stem}.invalid{raw_path.suffix}")
                invalid_path.write_text(raw_text, encoding="utf-8")
                raw_path.unlink()
                print(f"Question {index}: moved incomplete cached output to {invalid_path}.")

        if verification is None:
            print(f"Question {index}: checking slide {slide_number} with {MODEL}...", flush=True)
            evidence_description = (
                "the extracted text and image"
                if include_slide_image
                else "the extracted text"
            )
            prompt = (
                "Audit the question and answer against the cited slide. Split every factual "
                "statement in the answer into the smallest independently checkable claims. "
                "CRITICAL CLAIM EXTRACTION RULE: Only extract claims that are directly and explicitly stated "
                "in the answer. Do NOT invent, synthesize, or extrapolate hypothetical claims, contrastive "
                "definitions, or missing information that the answer did not actually assert. "
                "Also include any factual premise in the question that the answer depends on. "
                f"Judge each claim using only this slide's {evidence_description}; do not use "
                "outside knowledge. For each claim, return source='question' or 'answer', its "
                "claim text, one status, and the specific slide evidence. Use supported only "
                "when the slide directly supports the claim; use contradicted when the slide "
                "conflicts with it; unsupported when the slide does not say it; unclear when "
                "the text/image is ambiguous. For diagrams, cite visible labels or relationships. "
                "Do not treat an unanswered exercise as evidence for an answer. Keep evidence "
                "and the overall explanation concise. Include every answer claim.\n\n"
                f"Cited slide: {slide_number}\n"
                f"Question: {question['question']}\n"
                f"Answer: {question['answer']}\n\n"
                f"Extracted slide text:\n{slide['text']}"
            )
            if question.get("verification_hint"):
                prompt += f"\n\nReview focus: {question['verification_hint']}"
            image_paths = [Path(slide["image"])] if include_slide_image else None
            review = None
            for attempt in range(1, 3):
                attempt_prompt = prompt
                predict_tokens = 1024
                if attempt == 2:
                    attempt_prompt += (
                        "\n\nThe previous response was incomplete or invalid. Return one "
                        "complete JSON object. Keep each claim and evidence concise (at most one sentence)."
                    )
                    print(f"Question {index}: retrying an incomplete claim audit once.", flush=True)

                try:
                    raw_text = generate_json(
                        attempt_prompt,
                        ClaimReview.model_json_schema(),
                        image_paths,
                    )
                except RuntimeError as error:
                    raise SystemExit(f"Model call failed on question {index}: {error}") from error

                try:
                    review = ClaimReview.model_validate_json(raw_text)
                    break
                except ValidationError as error:
                    invalid_path = next_invalid_path(raw_path)
                    invalid_path.write_text(raw_text, encoding="utf-8")
                    if attempt == 2:
                        raise SystemExit(
                            f"Local model returned incomplete or invalid claim JSON twice for "
                            f"question {index}. Responses preserved at {invalid_path}."
                        ) from error
                    print(
                        f"Invalid claim audit preserved at {invalid_path}; retrying once.",
                        flush=True,
                    )

            assert review is not None
            raw_path.write_text(raw_text, encoding="utf-8")
            verification = {
                "supported": aggregate_status(review.claims),
                "explanation": review.explanation,
                "claims": [claim.model_dump() for claim in review.claims],
            }
            print(f"Question {index}: {verification['supported']} — {verification['explanation']}")
            for claim in review.claims:
                print(
                    f"  {claim.source} claim [{claim.status}]: {claim.claim} "
                    f"— Evidence: {claim.evidence}"
                )

        results.append({
            "source_section": question.get("source_section"),
            "question": question["question"],
            "answer": question["answer"],
            "type": question["type"],
            "source_slide": slide_number,
            "input_fingerprint": fingerprint,
            "verification": verification,
        })
        save_results()

    print(f"\nSaved verification results to {output_path}")
    print(f"Counts: {json.loads(output_path.read_text(encoding='utf-8'))['counts']}")


if __name__ == '__main__':
    main()
