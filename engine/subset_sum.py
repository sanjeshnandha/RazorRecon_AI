"""
Bounded subset-sum. Handles both directions of merged and split credits
deterministically -- the case most teams hand to a model.

Worst case C(10,4) = 210 subsets. The bound is what makes it provable rather
than heuristic: we enumerate EVERY subset within the bound and require exactly
one to hit the target. Two subsets that both sum correctly is ambiguity, and
ambiguity always beats confidence.
"""
from __future__ import annotations

from itertools import combinations


def find_unique_subset(candidates: list[tuple[str, int]], target: int,
                       max_candidates: int, max_size: int) -> tuple[list[str] | None, int]:
    """Return (the single subset of ids summing exactly to target, n_solutions).

    `candidates` is [(id, amount_paise)]. Returns (None, n) when there is no
    solution (n=0) or more than one (n>=2) -- both mean "do not auto-match".
    """
    if target <= 0 or not candidates:
        return None, 0
    pool = sorted(candidates, key=lambda x: (-x[1], x[0]))[:max_candidates]
    solutions: list[list[str]] = []
    for size in range(2, min(max_size, len(pool)) + 1):
        for combo in combinations(pool, size):
            if sum(a for _, a in combo) == target:
                solutions.append([i for i, _ in combo])
                if len(solutions) > 1:
                    return None, len(solutions)
    if len(solutions) == 1:
        return solutions[0], 1
    return None, len(solutions)
