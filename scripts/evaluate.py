import json
import math
from pathlib import Path


def precision_at_k(predicted: list[str], relevant: set[str], k: int) -> float:
    return len(set(predicted[:k]) & relevant) / k


def recall_at_k(predicted: list[str], relevant: set[str], k: int) -> float:
    return len(set(predicted[:k]) & relevant) / max(len(relevant), 1)


def ndcg_at_k(predicted: list[str], relevant: set[str], k: int) -> float:
    dcg = sum((1 if item in relevant else 0) / math.log2(index + 2) for index, item in enumerate(predicted[:k]))
    ideal = sum(1 / math.log2(index + 2) for index in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0


def reciprocal_rank(predicted: list[str], relevant: set[str]) -> float:
    return next((1 / (index + 1) for index, item in enumerate(predicted) if item in relevant), 0)


if __name__ == "__main__":
    journeys = json.loads(Path("evaluation/journeys.json").read_text(encoding="utf-8"))
    print(f"Loaded {len(journeys)} reproducible evaluation journeys.")
    print("Run against a configured environment from the admin evaluation endpoint or test suite.")

