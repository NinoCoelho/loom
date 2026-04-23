"""Model selection given a message category and provider registry.

:class:`ModelStrengths` encodes a model's relative strengths across
dimensions (speed, cost, reasoning, coding). :func:`choose_model` classifies
the message, scores each registered model against the category, and returns
the highest-scoring candidate. A cost bonus is applied so cheaper models
are preferred when strengths are equal.
"""

from __future__ import annotations

from loom.llm.registry import ProviderRegistry
from loom.routing.classifier import MessageCategory, classify_message


class ModelStrengths:
    def __init__(
        self,
        speed: int = 5,
        cost: int = 5,
        reasoning: int = 5,
        coding: int = 5,
    ) -> None:
        self.speed = speed
        self.cost = cost
        self.reasoning = reasoning
        self.coding = coding


def choose_model(
    message: str,
    registry: ProviderRegistry,
    strengths: dict[str, ModelStrengths] | None = None,
    default_model: str | None = None,
) -> str | None:
    if not registry.list_models():
        return default_model

    category = classify_message(message)
    strengths = strengths or {}

    candidates = registry.list_models()
    if not candidates:
        return default_model

    if len(candidates) == 1:
        return candidates[0]

    def _score(model_id: str) -> float:
        s = strengths.get(model_id, ModelStrengths())
        if category == MessageCategory.CODING:
            primary = s.coding
        elif category == MessageCategory.REASONING:
            primary = s.reasoning
        elif category == MessageCategory.TRIVIAL:
            primary = s.speed
        else:
            primary = (s.reasoning + s.coding) / 2
        cost_bonus = (10 - s.cost) * 0.1
        return primary + cost_bonus

    candidates.sort(key=_score, reverse=True)
    return candidates[0]
