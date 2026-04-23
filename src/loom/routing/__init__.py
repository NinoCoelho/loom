"""Model routing — classify messages and choose the best model.

:class:`MessageCategory` classifies a chat message by intent
(e.g. ``general``, ``code``, ``creative``). :func:`choose_model`
maps a category to a :class:`ModelStrengths` profile and returns the
best-fit model ID for the current provider.

Used by :attr:`AgentConfig.choose_model` to dynamically select models
per turn rather than locking to a single default.
"""

from loom.routing.classifier import (
    MessageCategory as MessageCategory,
)
from loom.routing.classifier import (
    classify_message as classify_message,
)
from loom.routing.router import (
    ModelStrengths as ModelStrengths,
)
from loom.routing.router import (
    choose_model as choose_model,
)

__all__ = [
    "MessageCategory",
    "classify_message",
    "ModelStrengths",
    "choose_model",
]
