import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
CUAD_JSON_PATH = DATA_DIR / "CUAD_v1.json"
OUTPUT_MANIFEST = DATA_DIR / "cuad_50_manifest.json"

# High-value legal risk categories for contract auditing
TARGET_CATEGORIES = [
    "Governing Law",
    "Termination For Convenience",
    "Cap On Liability",
    "Uncapped Liability",
    "Liquidated Damages",
    "Non-Compete",
    "Exclusivity",
    "Anti-Assignment",
    "Audit Rights",
    "Ip Ownership Assignment",
    "Change Of Control",
    "Post-Termination Services",
    "Warranty Duration",
    "Insurance",
]


def extract_category(question: str) -> str:
    """Extracts the CUAD category name from the SQuAD-formatted question string."""
    if 'related to "' in question:
        return question.split('related to "')[1].split('"')[0].strip()
    return "General"


def main():
    if not CUAD_JSON_PATH.exists():
        print(f"ERROR: Could not find {CUAD_JSON_PATH}")
        print("Download CUAD_v1.zip from https://zenodo.org/records/4595826 and place CUAD_v1.json in backend/benchmarks/data/")
        return

    print(f"Loading {CUAD_JSON_PATH} ...")
    with open(CUAD_JSON_PATH, "r", encoding="utf-8") as f:
        cuad = json.load(f)

    selected = []
    seen_titles = set()
    category_counts = {}

    for entry in cuad.get("data", []):
        if len(selected) >= 50:
            break

        title = entry.get("title", "").strip()
        if not title or title in seen_titles:
            continue

        paragraphs = entry.get("paragraphs", [])
        if not paragraphs:
            continue

        context = paragraphs[0].get("context", "")
        # Keep contracts within a practical length (5,000 to 60,000 chars ~ 3 to 35 pages)
        # so indexing stays clean and fast on free-tier embeddings
        if not (5_000 <= len(context) <= 60_000):
            continue

        qas = paragraphs[0].get("qas", [])
        best_qa = None

        # Prefer categories with fewer samples so our 50 contracts are balanced across risk types
        candidates = []
        for qa in qas:
            if qa.get("is_impossible", True):
                continue
            answers = qa.get("answers", [])
            if not answers:
                continue
            ans_text = answers[0].get("text", "").strip()
            if len(ans_text) < 25:
                continue

            cat = extract_category(qa.get("question", ""))
            if cat in TARGET_CATEGORIES:
                candidates.append((category_counts.get(cat, 0), cat, qa, ans_text))

        if not candidates:
            continue

        candidates.sort(key=lambda x: x[0])
        _, chosen_cat, best_qa, gt_text = candidates[0]

        safe_filename = f"cuad_{len(selected) + 1:02d}_{title[:40].replace(' ', '_')}.txt"
        safe_filename = "".join(c if c.isalnum() or c in "._-" else "_" for c in safe_filename)

        # Clean natural-language legal audit question for the chosen category
        clean_query = (
            f"Analyze the '{chosen_cat}' clause in this contract. "
            f"Quote the exact relevant text, explain the legal risk, and provide a safe revision."
        )

        selected.append({
            "index": len(selected) + 1,
            "cuad_title": title,
            "filename": safe_filename,
            "category": chosen_cat,
            "query": clean_query,
            "ground_truth_span": gt_text,
            "all_ground_truth_spans": [a["text"].strip() for a in best_qa["answers"] if a.get("text")],
            "contract_char_length": len(context),
            "contract_text": context,
        })

        seen_titles.add(title)
        category_counts[chosen_cat] = category_counts.get(chosen_cat, 0) + 1

    with open(OUTPUT_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(selected, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Successfully selected {len(selected)} distinct real CUAD contracts!")
    print(f"Saved manifest to: {OUTPUT_MANIFEST}\n")
    print("Category distribution across the 50 contracts:")
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        print(f"  - {cat}: {count}")


if __name__ == "__main__":
    main()