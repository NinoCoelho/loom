"""Entity extraction prompt templates, response parsing, and extraction orchestration."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
Extract entities and relationships from the following text.

Entity types to look for: {entity_types}

For each entity provide:
- name: the canonical name, capitalized. MUST be a single line of plain
  text, ≤ 60 characters. Must NOT contain newlines, pipes (|), markdown
  link syntax (](), URLs, code, table rows, mermaid field declarations
  (e.g. "int pk PK"), or list bullets. If the source mentions an entity
  inside a code block, table, or URL, extract a clean human-readable
  name only — do not include the surrounding syntax. If you cannot
  produce a clean name, omit the entity.
- type: one of the entity types above
- description: brief description based on context (≤ 1 sentence)

For relationships use one of these relation types when they fit: {core_relations}
Otherwise use the relation type that best describes the relationship and set "custom" to true.

For each relationship provide:
- head: name of the source entity (same constraints as entity name)
- relation: the relation type (snake_case, ≤ 30 chars, no spaces or punctuation)
- tail: name of the target entity (same constraints as entity name)
- description: natural language description of the relationship
- strength: integer 1-10 indicating relationship strength

Skip any entity or relationship you are not confident in. Quality over quantity.

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

_NAME_MAX_LEN = 80
_NAME_REJECT_SUBSTRINGS = ("](", "://", "```")
_NAME_MERMAID_TOKENS = re.compile(r"\b(?:PK|FK|pk|fk)\b")
_NAME_HAS_LETTER = re.compile(r"[A-Za-zÀ-ÿ]")


def _sanitize_entity_name(raw: str) -> str | None:
    """Return a cleaned entity name, or ``None`` if it should be rejected.

    Rejects multi-line names, markdown table/link/URL fragments, mermaid
    field declarations, and over-long or letter-less strings. The LLM
    extractor is the trust boundary here: garbage that lands in the DB
    becomes orphan nodes and noisy edges in the knowledge graph.
    """
    if not raw:
        return None
    s = raw.strip()
    s = s.strip("*_`> -")
    if not s or len(s) > _NAME_MAX_LEN or len(s) < 2:
        return None
    if any(c in s for c in "\n\r\t|"):
        return None
    if any(sub in s for sub in _NAME_REJECT_SUBSTRINGS):
        return None
    if not _NAME_HAS_LETTER.search(s):
        return None
    if _NAME_MERMAID_TOKENS.search(s):
        return None
    return s


def ontology_relation_ok(relation: str, core_relations: list[str], allow_custom: bool) -> bool:
    if relation in core_relations:
        return True
    return allow_custom


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


class GraphRAGExtractor:
    def __init__(
        self,
        entity_graph: Any,
        llm_provider: Any,
        config: Any,
    ) -> None:
        self._entity_graph = entity_graph
        self._llm = llm_provider
        self._config = config

    async def extract(self, chunks: list[Any]) -> None:
        await self._extract_entities(chunks)

    async def _extract_entities(self, chunks: list[Any]) -> None:
        from loom.types import ChatMessage, Role

        ontology = self._config.ontology
        entity_types = ", ".join(ontology.entity_types)
        core_relations = ", ".join(ontology.core_relations)

        for chunk in chunks:
            prompt = _EXTRACTION_PROMPT.format(
                entity_types=entity_types,
                core_relations=core_relations,
                text=chunk.content[:3000],
            )
            messages = [ChatMessage(role=Role.USER, content=prompt)]
            try:
                resp = await self._llm.chat(messages)
            except Exception:
                logger.warning("Entity extraction LLM call failed", exc_info=True)
                continue

            parsed = parse_extraction_response(resp.message.content or "")
            self._store_extraction(parsed, chunk.id)

            for _ in range(self._config.extraction.max_gleanings):
                glean_prompt = _GLEAN_PROMPT.format(text=chunk.content[:3000])
                glean_messages = [
                    ChatMessage(role=Role.USER, content=prompt),
                    resp.message,
                    ChatMessage(role=Role.USER, content=glean_prompt),
                ]
                try:
                    glean_resp = await self._llm.chat(glean_messages)
                except Exception:
                    break
                glean_parsed = parse_extraction_response(glean_resp.message.content or "")
                self._store_extraction(glean_parsed, chunk.id)

    def _store_extraction(self, parsed: dict[str, Any], chunk_id: str) -> None:
        aliases = self._config.ontology.aliases
        entity_name_to_id: dict[str, int] = {}

        for ent in parsed.get("entities", []):
            name = _sanitize_entity_name(ent.get("name", ""))
            etype = ent.get("type", "concept").strip().lower()
            if not name:
                continue
            if etype not in self._config.ontology.entity_types:
                etype = "concept"
            eid = self._entity_graph.resolve_entity(name, etype, aliases)
            entity_name_to_id[name.lower()] = eid
            if ent.get("description"):
                existing = self._entity_graph.get_entity(eid)
                if existing and not existing.description:
                    self._entity_graph.set_entity_description(eid, ent["description"][:500])
            self._entity_graph.add_mention(eid, chunk_id)

        for rel in parsed.get("relations", []):
            head = _sanitize_entity_name(rel.get("head", ""))
            tail = _sanitize_entity_name(rel.get("tail", ""))
            relation = rel.get("relation", "related_to").strip()
            desc = rel.get("description", "").strip()
            strength = float(rel.get("strength", 5))
            if not head or not tail:
                continue

            head_id = entity_name_to_id.get(head.lower())
            tail_id = entity_name_to_id.get(tail.lower())

            if head_id is None:
                head_id = self._entity_graph.resolve_entity(head, "concept", aliases)
                entity_name_to_id[head.lower()] = head_id
                self._entity_graph.add_mention(head_id, chunk_id)
            if tail_id is None:
                tail_id = self._entity_graph.resolve_entity(tail, "concept", aliases)
                entity_name_to_id[tail.lower()] = tail_id
                self._entity_graph.add_mention(tail_id, chunk_id)

            if not ontology_relation_ok(
                relation,
                self._config.ontology.core_relations,
                self._config.ontology.allow_custom_relations,
            ):
                relation = "related_to"

            self._entity_graph.add_triple(head_id, relation, tail_id, chunk_id, desc, strength)
