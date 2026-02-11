from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from republic import LLM
from republic.tape.context import LAST_ANCHOR, TapeContext
from republic.tape.entries import TapeEntry
from republic.tape.store import TapeStore

from endless_context.tape_store import SeekDBTapeStore

DEFAULT_SYSTEM_PROMPT = (
    "You are a tape-first assistant. Keep answers concise, grounded in recorded facts, "
    "and maintain continuity with handoff anchors."
)
AUTO_BOOTSTRAP_ANCHOR = "handoff:auto-bootstrap"
AUTO_BOOTSTRAP_STATE = {
    "phase": "Bootstrap",
    "summary": "Auto-created bootstrap anchor for context slicing.",
}

ViewMode = Literal["full", "latest", "from-anchor"]


@dataclass(frozen=True)
class AnchorState:
    entry_id: int
    name: str
    label: str
    summary: str
    facts: list[str]
    created_at: str | None


@dataclass(frozen=True)
class ConversationSnapshot:
    tape_name: str
    entries: list[TapeEntry]
    anchors: list[AnchorState]
    active_anchor: AnchorState | None
    context_entries: list[TapeEntry]
    estimated_tokens: int

    @property
    def total_entries(self) -> int:
        return len(self.entries)

    @property
    def context_entry_count(self) -> int:
        return len(self.context_entries)

    @property
    def messages(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for entry in self.entries:
            if entry.kind != "message":
                continue
            role = entry.payload.get("role")
            content = entry.payload.get("content")
            if role in {"user", "assistant"} and isinstance(content, str):
                result.append({"role": role, "content": content})
        return result


class SimpleAgent:
    """Republic-based chat agent with tape snapshots for Gradio UI."""

    def __init__(
        self,
        user_id: str = "default",
        agent_id: str = "endless-context",
        tape_name: str | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        llm: LLM | Any | None = None,
        tape_store: TapeStore | None = None,
    ) -> None:
        self.user_id = user_id
        self.agent_id = agent_id
        self.tape_name = tape_name or f"{agent_id}:{user_id}"
        self.system_prompt = system_prompt
        self.llm = llm or self._build_default_llm(tape_store=tape_store)

    def reply(
        self,
        message: str,
        history: Iterable[dict[str, Any]] | None = None,
        *,
        view_mode: ViewMode = "latest",
        anchor_name: str | None = None,
    ) -> str:
        if not isinstance(message, str):
            raise ValueError("message must be a string")

        resolved_mode, resolved_anchor_name, _, _ = self._resolve_view(
            view_mode=view_mode,
            anchor_name=anchor_name,
            ensure_anchor=view_mode != "full",
        )
        context = self._context_for_view(view_mode=resolved_mode, anchor_name=resolved_anchor_name)
        result = self.llm.chat(
            prompt=message,
            tape=self.tape_name,
            context=context,
            system_prompt=self.system_prompt,
        )
        if result.error is not None:
            return f"Error: {result.error.message}"
        if result.value is None:
            return ""
        return str(result.value)

    def handoff(
        self,
        name: str,
        *,
        phase: str = "",
        summary: str = "",
        facts: list[str] | None = None,
    ) -> str:
        normalized = self._normalize_anchor_name(name)
        state: dict[str, Any] = {}
        if phase.strip():
            state["phase"] = phase.strip()
        if summary.strip():
            state["summary"] = summary.strip()
        if facts:
            clean_facts = [item.strip() for item in facts if item.strip()]
            if clean_facts:
                state["facts"] = clean_facts
        self.llm.tape(self.tape_name).handoff(normalized, state=state or None)
        return normalized

    def reset(self) -> None:
        self.llm.tape(self.tape_name).reset()

    def snapshot(
        self,
        *,
        view_mode: ViewMode = "latest",
        anchor_name: str | None = None,
    ) -> ConversationSnapshot:
        resolved_mode, resolved_anchor_name, entries, anchors = self._resolve_view(
            view_mode=view_mode,
            anchor_name=anchor_name,
            ensure_anchor=view_mode != "full",
        )
        active_anchor, context_entries = select_context_entries(
            entries,
            anchors,
            resolved_mode,
            resolved_anchor_name,
        )
        return ConversationSnapshot(
            tape_name=self.tape_name,
            entries=entries,
            anchors=anchors,
            active_anchor=active_anchor,
            context_entries=context_entries,
            estimated_tokens=estimate_tokens(context_entries),
        )

    def _read_entries(self) -> list[TapeEntry]:
        return self.llm.tape(self.tape_name).read_entries()

    def _create_bootstrap_anchor(self) -> tuple[list[TapeEntry], list[AnchorState], AnchorState | None]:
        self.llm.tape(self.tape_name).handoff(AUTO_BOOTSTRAP_ANCHOR, state=AUTO_BOOTSTRAP_STATE)
        entries = self._read_entries()
        anchors = extract_anchors(entries)
        created = find_anchor_by_name(anchors, AUTO_BOOTSTRAP_ANCHOR)
        if created is None and anchors:
            created = anchors[-1]
        return entries, anchors, created

    def _resolve_view(
        self,
        *,
        view_mode: ViewMode,
        anchor_name: str | None,
        ensure_anchor: bool,
    ) -> tuple[ViewMode, str | None, list[TapeEntry], list[AnchorState]]:
        entries = self._read_entries()
        anchors = extract_anchors(entries)

        if view_mode == "full":
            return "full", None, entries, anchors

        if view_mode == "latest":
            if not anchors and ensure_anchor:
                entries, anchors, _ = self._create_bootstrap_anchor()
            resolved_anchor_name = anchors[-1].name if anchors else None
            return "latest", resolved_anchor_name, entries, anchors

        target = find_anchor_by_name(anchors, anchor_name) if anchor_name else None
        if target is None and anchors:
            target = anchors[-1]
        if target is None and ensure_anchor:
            entries, anchors, target = self._create_bootstrap_anchor()
        resolved_anchor_name = target.name if target else None
        return "from-anchor", resolved_anchor_name, entries, anchors

    @staticmethod
    def _context_for_view(view_mode: ViewMode, anchor_name: str | None) -> TapeContext:
        if view_mode == "full":
            return TapeContext(anchor=None)
        if view_mode == "from-anchor" and anchor_name:
            return TapeContext(anchor=anchor_name)
        return TapeContext(anchor=LAST_ANCHOR)

    @staticmethod
    def _normalize_anchor_name(name: str) -> str:
        raw = name.strip()
        if not raw:
            raise ValueError("anchor name cannot be empty")
        if raw.startswith("handoff:") or raw.startswith("phase:"):
            return raw
        safe = raw.lower().replace(" ", "-")
        return f"handoff:{safe}"

    @staticmethod
    def _build_default_llm(tape_store: TapeStore | None) -> LLM:
        store = tape_store or SeekDBTapeStore.from_env()
        model = os.getenv("REPUBLIC_MODEL")
        provider = (os.getenv("LLM_PROVIDER") or "").strip().lower()
        llm_model = os.getenv("LLM_MODEL")

        # Qwen is typically accessed through OpenAI-compatible endpoints.
        if model and model.startswith("qwen:"):
            model = f"openai:{model.split(':', 1)[1]}"
        if not model:
            effective_provider = provider
            if provider in {"qwen", "dashscope"}:
                effective_provider = "openai"
            if provider and llm_model and ":" not in llm_model:
                model = f"{effective_provider}:{llm_model}"
            else:
                model = llm_model
        if not model:
            model = "openai:gpt-4o-mini"

        api_key = os.getenv("REPUBLIC_API_KEY") or os.getenv("LLM_API_KEY")
        api_base = os.getenv("REPUBLIC_API_BASE") or os.getenv("LLM_API_BASE")
        if not api_base and provider in {"qwen", "dashscope"}:
            api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        verbose = int(os.getenv("REPUBLIC_VERBOSE", "0"))

        kwargs: dict[str, Any] = {
            "tape_store": store,
            "verbose": verbose,
        }
        if model:
            kwargs["model"] = model
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base
        return LLM(**kwargs)


def extract_anchors(entries: list[TapeEntry]) -> list[AnchorState]:
    anchors: list[AnchorState] = []
    for entry in entries:
        if entry.kind != "anchor":
            continue
        name = str(entry.payload.get("name", "")).strip()
        if not name:
            continue
        state = entry.payload.get("state")
        state_dict = state if isinstance(state, dict) else {}
        phase = state_dict.get("phase")
        label = phase.strip() if isinstance(phase, str) and phase.strip() else name.split(":")[-1]
        summary = state_dict.get("summary")
        summary_text = summary.strip() if isinstance(summary, str) else ""
        raw_facts = state_dict.get("facts")
        facts = [str(item).strip() for item in raw_facts if str(item).strip()] if isinstance(raw_facts, list) else []
        created_at = entry.meta.get("created_at") if isinstance(entry.meta, dict) else None
        anchors.append(
            AnchorState(
                entry_id=entry.id,
                name=name,
                label=label or "anchor",
                summary=summary_text,
                facts=facts,
                created_at=created_at if isinstance(created_at, str) else None,
            )
        )
    return anchors


def select_context_entries(
    entries: list[TapeEntry],
    anchors: list[AnchorState],
    view_mode: ViewMode,
    anchor_name: str | None,
) -> tuple[AnchorState | None, list[TapeEntry]]:
    if view_mode == "full":
        return None, entries

    if view_mode == "latest":
        if not anchors:
            return None, entries
        anchor = anchors[-1]
        return anchor, entries_after_id(entries, anchor.entry_id)

    if view_mode == "from-anchor":
        target = find_anchor_by_name(anchors, anchor_name) if anchor_name else None
        if target is None:
            if not anchors:
                return None, entries
            target = anchors[-1]
        return target, entries_after_id(entries, target.entry_id)

    return None, entries


def find_anchor_by_name(anchors: list[AnchorState], name: str) -> AnchorState | None:
    for anchor in reversed(anchors):
        if anchor.name == name:
            return anchor
    return None


def entries_after_id(entries: list[TapeEntry], entry_id: int) -> list[TapeEntry]:
    for index, entry in enumerate(entries):
        if entry.id == entry_id:
            return entries[index + 1 :]
    return entries


def estimate_tokens(entries: list[TapeEntry]) -> int:
    total = 0
    for entry in entries:
        if entry.kind == "message":
            content = entry.payload.get("content")
            if isinstance(content, str):
                total += max(1, len(content) // 4)
                continue
        total += 10
    return total
