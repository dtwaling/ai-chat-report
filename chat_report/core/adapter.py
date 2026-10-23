"""HarnessAdapter protocol, ExtractOptions, and adapter registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from chat_report.core.ir import ResolvedTarget, SessionIR


class SessionNotFoundError(LookupError):
    """Raised when a session or request UUID cannot be resolved."""


class StoreAccessError(RuntimeError):
    """Raised when a required SQLite database is missing, locked, or unreadable."""


@dataclass
class ExtractOptions:
    state_db: Path | None = None
    tracking_db: Path | None = None
    workspace: Path | None = None
    include_subagents: bool = True
    list_include_subagents: bool = False
    include_orphans: bool = False
    force_id_kind: str | None = None  # None | "chat-id" | "request-id"
    extra_denial_markers: list[str] = field(default_factory=list)
    verbose: bool = False


@dataclass
class SessionListEntry:
    session_id: str
    title: str | None
    created_at_ms: int | None
    last_updated_at_ms: int | None
    is_subagent: bool
    subagent_type: str | None
    model_name: str | None


@runtime_checkable
class HarnessAdapter(Protocol):
    """Protocol implemented by each agent harness adapter (e.g. CursorAdapter)."""

    harness_name: str

    def list_recent(self, limit: int, options: ExtractOptions) -> list[SessionListEntry]:
        ...

    def resolve_id(self, raw_id: str, options: ExtractOptions) -> ResolvedTarget:
        ...

    def extract_session(
        self,
        session_id: str,
        options: ExtractOptions,
        *,
        resolved_from: ResolvedTarget | None = None,
    ) -> SessionIR:
        ...


ADAPTER_REGISTRY: dict[str, Callable[[], HarnessAdapter]] = {}


def register_adapter(name: str, factory: Callable[[], HarnessAdapter]) -> None:
    """Register a harness adapter factory by lowercase name."""
    key = name.strip().lower()
    if not key:
        raise ValueError("Adapter name cannot be empty.")
    ADAPTER_REGISTRY[key] = factory


def get_adapter(harness_name: str = "cursor") -> HarnessAdapter:
    """Look up and instantiate a registered harness adapter."""
    key = (harness_name or "").strip().lower()
    factory = ADAPTER_REGISTRY.get(key)
    if factory is None:
        registered = sorted(ADAPTER_REGISTRY.keys())
        raise ValueError(f"Unsupported harness '{harness_name}'. Registered: {registered}")
    return factory()
