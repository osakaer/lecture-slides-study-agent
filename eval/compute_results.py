"""Summarize human ratings, verifier errors, and paired repair outcomes."""

import csv
from collections import Counter, defaultdict
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
EVAL_DIR = PROJECT_DIR / "eval"
RATINGS_PATH = EVAL_DIR / "ratings.csv"
KEY_PATH = EVAL_DIR / "key.csv"
RELEVANCE_VALUES = {"relevant", "trivial", "wrong"}
SUPPORT_VALUES = {"yes", "partial", "no"}
SUPPORT_SCORE = {"no": 0, "partial": 1, "yes": 2}


def load_csv(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        raise SystemExit(f"Required file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as file:
        return {row["rating_id"]: row for row in csv.DictReader(file)}


def percent(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a (0 rated items)"
    return f"{numerator}/{denominator} ({numerator / denominator:.1%})"


def main() -> None:
    ratings = load_csv(RATINGS_PATH)
    key = load_csv(KEY_PATH)
    if not ratings:
        raise SystemExit("ratings.csv contains no rating rows.")
    if ratings.keys() != key.keys():
        raise SystemExit("ratings.csv and key.csv have different rating IDs.")

    joined = []
    for rating_id, row in ratings.items():
        relevance = row.get("relevance", "").strip().lower()
        support = row.get("slide_supports", "").strip().lower()
        if relevance not in RELEVANCE_VALUES or support not in SUPPORT_VALUES:
            raise SystemExit(
                f"Complete valid relevance and slide_supports labels for {rating_id}: "
                "relevance must be relevant/trivial/wrong; slide_supports must be yes/partial/no."
            )
        joined.append({**row, **key[rating_id], "relevance": relevance, "slide_supports": support})

    first_pass = [row for row in joined if row.get("version") == "first_pass"]
    repaired = [row for row in joined if row.get("version") == "repaired"]
    if not first_pass:
        raise SystemExit("No first-pass ratings were found in key.csv.")

    relevance_counts = Counter(row["relevance"] for row in first_pass)
    support_counts = Counter(row["slide_supports"] for row in first_pass)
    acceptable_first_pass = sum(
        row["relevance"] == "relevant" and row["slide_supports"] == "yes"
        for row in first_pass
    )

    audited = [
        row for row in first_pass
        if row.get("verifier_supported") in SUPPORT_VALUES
    ]
    human_supported = [row for row in audited if row["slide_supports"] == "yes"]
    human_unsupported = [row for row in audited if row["slide_supports"] in {"partial", "no"}]
    misses = sum(row["verifier_supported"] == "yes" for row in human_unsupported)
    catches = len(human_unsupported) - misses
    false_alarms = sum(row["verifier_supported"] != "yes" for row in human_supported)

    by_pair: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for row in joined:
        by_pair[row["pair_id"]][row["version"]] = row
    comparable = [
        pair for pair in by_pair.values()
        if "first_pass" in pair and "repaired" in pair
    ]
    effects = Counter()
    for pair in comparable:
        before = SUPPORT_SCORE[pair["first_pass"]["slide_supports"]]
        after = SUPPORT_SCORE[pair["repaired"]["slide_supports"]]
        effects["improved" if after > before else "worse" if after < before else "same"] += 1
    repaired_trivial = sum(row["relevance"] == "trivial" for row in repaired)

    print("First-pass question quality")
    for label in ("relevant", "trivial", "wrong"):
        print(f"  Relevance {label}: {percent(relevance_counts[label], len(first_pass))}")
    for label in ("yes", "partial", "no"):
        print(f"  Slide support {label}: {percent(support_counts[label], len(first_pass))}")
    print(
        "  Relevant and fully supported: "
        f"{percent(acceptable_first_pass, len(first_pass))}"
    )

    print("\nVerifier performance against human first-pass ratings")
    print(f"  Questions with verifier verdicts: {len(audited)}/{len(first_pass)}")
    print(f"  Miss rate (human partial/no called yes): {percent(misses, len(human_unsupported))}")
    print(f"  Catch rate (human partial/no not called yes): {percent(catches, len(human_unsupported))}")
    print(f"  False-alarm rate (human yes called partial/no): {percent(false_alarms, len(human_supported))}")

    print("\nRepair effect (paired human support ratings)")
    print(f"  Changed questions rated in both versions: {len(comparable)}")
    for label in ("improved", "same", "worse"):
        print(f"  Support {label}: {effects[label]}")
    print(f"  Repaired versions rated trivial: {percent(repaired_trivial, len(repaired))}")
    print("\nThis compares first-pass generation with the post-verification/repair output; it is not a naive-prompt baseline.")


if __name__ == "__main__":
    main()
