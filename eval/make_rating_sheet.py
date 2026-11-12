"""Create a blinded human-rating sheet from evaluation run snapshots."""

import csv
import json
import random
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
EVAL_DIR = PROJECT_DIR / "eval"
MANIFEST_PATH = EVAL_DIR / "run_manifest.json"
RATINGS_PATH = EVAL_DIR / "ratings.csv"
KEY_PATH = EVAL_DIR / "key.csv"
RATING_FIELDS = [
    "rating_id",
    "deck_name",
    "source_slide",
    "slide_text",
    "slide_image",
    "question",
    "answer",
    "relevance",
    "slide_supports",
]


def read_json(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Required evaluation file is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def question_key(question: dict) -> tuple[int | None, int]:
    return question.get("source_section"), question.get("candidate_number", 1)


def verification_map(path: Path) -> dict[tuple, str]:
    if not path.is_file():
        return {}
    data = read_json(path)
    return {
        (
            result.get("source_section"),
            result.get("question"),
            result.get("answer"),
            result.get("source_slide"),
        ): result.get("verification", {}).get("supported")
        for result in data.get("results", [])
    }


def main() -> None:
    manifest = read_json(MANIFEST_PATH)
    rating_items: list[dict] = []
    key_items: list[dict] = []

    for run in manifest.get("runs", []):
        if run.get("return_code") != 0:
            raise SystemExit(f"Evaluation run failed for {run.get('deck_name')}.")
        run_dir = PROJECT_DIR / run["run_dir"]
        question_dir = run_dir / "questions"
        first_set = read_json(question_dir / "curated_set.pre_repair.json")
        current_set = read_json(question_dir / "curated_set.json")
        slides = read_json(run_dir / "extracted.json")
        slides_by_number = {slide["slide"]: slide for slide in slides}
        first_verifications = verification_map(question_dir / "verification.pre_repair.json")
        final_verifications = verification_map(question_dir / "verification.json")
        current_questions = {
            question_key(question): question
            for question in current_set.get("questions", [])
        }

        for first in first_set.get("questions", []):
            key = question_key(first)
            slide_number = first["source_slide"]
            slide = slides_by_number.get(slide_number)
            if slide is None:
                raise SystemExit(
                    f"Slide {slide_number} not found while building ratings for {run['deck_name']}."
                )
            pair_id = f"{Path(run['run_dir']).name}:{key[0]}:{key[1]}"
            versions = [("first_pass", first, first_verifications)]
            repaired = current_questions.get(key)
            if repaired and (
                repaired.get("question") != first.get("question")
                or repaired.get("answer") != first.get("answer")
            ):
                versions.append(("repaired", repaired, final_verifications))

            for version, question, verdicts in versions:
                lookup_key = (
                    question.get("source_section"),
                    question["question"],
                    question["answer"],
                    question["source_slide"],
                )
                rating_items.append({
                    "deck_name": run["deck_name"],
                    "source_slide": question["source_slide"],
                    "slide_text": slide["text"],
                    "slide_image": slide["image"],
                    "question": question["question"],
                    "answer": question["answer"],
                    "relevance": "",
                    "slide_supports": "",
                })
                key_items.append({
                    "pair_id": pair_id,
                    "version": version,
                    "verifier_supported": verdicts.get(lookup_key, ""),
                })

    if not rating_items:
        raise SystemExit("No questions found in the successful evaluation runs.")

    pairs = list(zip(rating_items, key_items))
    random.SystemRandom().shuffle(pairs)
    rating_items, key_items = map(list, zip(*pairs))
    for index, item in enumerate(rating_items, start=1):
        rating_id = f"R{index:04d}"
        item["rating_id"] = rating_id
        key_items[index - 1]["rating_id"] = rating_id

    with RATINGS_PATH.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=RATING_FIELDS)
        writer.writeheader()
        writer.writerows(rating_items)
    with KEY_PATH.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["rating_id", "pair_id", "version", "verifier_supported"],
        )
        writer.writeheader()
        writer.writerows(key_items)

    print(f"Created blinded sheet with {len(rating_items)} rows: {RATINGS_PATH}")
    print("Fill relevance and slide_supports in ratings.csv. Keep key.csv closed until ratings are complete.")


if __name__ == "__main__":
    main()
