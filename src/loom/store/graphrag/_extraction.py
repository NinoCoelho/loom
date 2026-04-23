"""Entity extraction prompt templates and response parsing."""

from __future__ import annotations

import json
import re
from typing import Any

_EXTRACTION_PROMPT = """\
Extract entities and relationships from the following text.

Entity types to look for: {entity_types}

For each entity provide:
- name: the canonical name, capitalized
- type: one of the entity types above
- description: brief description based on context

For relationships use one of these relation types when they fit: {core_relations}
Otherwise use the relation type that best describes the relationship and set "custom" to true.

For each relationship provide:
- head: name of the source entity
- relation: the relation type
- tail: name of the target entity
- description: natural language description of the relationship
- strength: integer 1-10 indicating relationship strength

Text:
{text}

Respond with ONLY valid JSON in this exact format (no markdown fences):
{{"entities": [{{"name": "...", "type": "...", "description": "..."}}],
 "relations": [{{"head": "...", "relation": "...", "tail": "..."
                 , "description": "...", "strength": 5, "custom": false}}]}}\
"""

_GLEAN_PROMPT = """\
Many entities and relationships were missed in the previous extraction.
Review the text again and extract any additional entities and relationships
that were missed. Use the same JSON format.

Text:
{text}

Respond with ONLY valid JSON:\
"""


def parse_extraction_response(text: str) -> dict[str, Any]:
    """Parse an LLM extraction response into an entity/relation dict."""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    return {"entities": [], "relations": []}
