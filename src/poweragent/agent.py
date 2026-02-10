from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from powermem import Memory, auto_config

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant. Keep responses concise."


class SimpleAgent:
    def __init__(
        self,
        user_id: str = "default",
        agent_id: str = "poweragent",
        memory_limit: int = 5,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        memory: Optional[Memory] = None,
    ) -> None:
        self.user_id = user_id
        self.agent_id = agent_id
        self.memory_limit = memory_limit
        self.system_prompt = system_prompt
        self.memory = memory or Memory(config=auto_config())

    def reply(self, message: str, history: Optional[Iterable[Dict[str, Any]]] = None) -> str:
        if not isinstance(message, str):
            raise ValueError("message must be a string")

        messages = self._build_messages(message, history)
        try:
            response = self.memory.llm.generate_response(messages=messages)
        except Exception as exc:
            logger.exception("LLM response failed")
            return f"Error: {exc}"

        self._store_turn(message, response)
        return response

    def _build_messages(self, message: str, history: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = [{"role": "system", "content": self.system_prompt}]

        context = self._search_context(message)
        if context:
            messages.append({"role": "system", "content": context})

        messages.extend(self._normalize_history(history))
        messages.append({"role": "user", "content": message})
        return messages

    def _normalize_history(self, history: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        for item in history or []:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                normalized.append({"role": role, "content": content.strip()})
        return normalized

    def _search_context(self, query: str) -> str:
        if not query.strip():
            return ""

        try:
            results = self.memory.search(
                query=query,
                user_id=self.user_id,
                agent_id=self.agent_id,
                limit=self.memory_limit,
            )
        except Exception:
            logger.exception("Memory search failed")
            return ""

        items = results.get("results") if isinstance(results, dict) else None
        if not items:
            return ""

        lines: List[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = (item.get("memory") or "").strip()
            if text:
                lines.append(f"- {text}")

        if not lines:
            return ""

        return "Relevant memories:\n" + "\n".join(lines)

    def _store_turn(self, message: str, response: str) -> None:
        try:
            self.memory.add(
                messages=[
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": response},
                ],
                user_id=self.user_id,
                agent_id=self.agent_id,
                infer=False,
            )
        except Exception:
            logger.exception("Memory add failed")
