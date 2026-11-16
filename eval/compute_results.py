"""Compare baseline and pipeline question ratings and summarize verifier/repair results."""

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


def method_metrics(rows: list[dict]) -> dict:
    relevant = sum(row["relevance"] == "relevant" for row in rows)
    supported = sum(row["slide_supports"] == "yes" for row in rows)
    quality = sum(
        row["relevance"] == "relevant" and row["slide_supports"] == "yes"
        for row in rows
    )
    return {
        "n": len(rows),
        "relevant": relevant,
        "supported": supported,
        "quality": quality,
    }


def rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


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
        joined.append({
            **row,
            **key[rating_id],
            "method": key[rating_id].get("method") or "pipeline",
            "relevance": relevance,
            "slide_supports": support,
        })

    baseline = [row for row in joined if row["method"] == "baseline"]
    first_pass = [
        row for row in joined
        if row["method"] == "pipeline" and row.get("version") == "first_pass"
    ]
    repaired = [
        row for row in joined
        if row["method"] == "pipeline" and row.get("version") == "repaired"
    ]
    if not first_pass:
        raise SystemExit("No first-pass ratings were found in key.csv.")
    if not baseline:
        raise SystemExit(
            "No baseline ratings were found. Rerun eval/run_all.py and "
            "eval/make_rating_sheet.py to include the one-prompt baseline."
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
        pair_id = row.get("pair_id", "")
        if pair_id:
            by_pair[pair_id][row["version"]] = row
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

    print("Question quality by method")
    method_rows = [
        ("Naive single-prompt baseline", baseline),
        ("Pipeline first pass", first_pass),
        ("Pipeline after repair", repaired),
    ]
    for label, rows in method_rows:
        if not rows:
            print(f"  {label}: no changed questions to rate")
            continue
        metrics = method_metrics(rows)
        print(
            f"  {label} (n={metrics['n']}): relevant "
            f"{percent(metrics['relevant'], metrics['n'])}; fully supported "
            f"{percent(metrics['supported'], metrics['n'])}; relevant and fully "
            f"supported {percent(metrics['quality'], metrics['n'])}"
        )

    baseline_rate = rate(method_metrics(baseline)["quality"], len(baseline))
    pipeline_rate = rate(method_metrics(first_pass)["quality"], len(first_pass))
    if baseline_rate is not None and pipeline_rate is not None:
        delta = (pipeline_rate - baseline_rate) * 100
        print(f"  Pipeline first-pass quality change vs baseline: {delta:+.1f} percentage points")

    print("\nPrimary metric by deck (relevant and fully supported)")
    deck_names = sorted({row["deck_name"] for row in baseline + first_pass})
    for deck_name in deck_names:
        baseline_deck = [row for row in baseline if row["deck_name"] == deck_name]
        pipeline_deck = [row for row in first_pass if row["deck_name"] == deck_name]
        baseline_metrics = method_metrics(baseline_deck)
        pipeline_metrics = method_metrics(pipeline_deck)
        baseline_percent = percent(baseline_metrics["quality"], baseline_metrics["n"])
        pipeline_percent = percent(pipeline_metrics["quality"], pipeline_metrics["n"])
        if baseline_metrics["n"] and pipeline_metrics["n"]:
            change = (
                rate(pipeline_metrics["quality"], pipeline_metrics["n"])
                - rate(baseline_metrics["quality"], baseline_metrics["n"])
            ) * 100
            delta_text = f"{change:+.1f} pp"
        else:
            delta_text = "n/a"
        print(
            f"  {deck_name}: baseline {baseline_percent}; "
            f"pipeline {pipeline_percent}; change {delta_text}"
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


if __name__ == "__main__":
    main()
