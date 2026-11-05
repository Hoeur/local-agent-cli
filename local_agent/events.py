"""Small, UI-independent runtime event contract (observers cannot fail a turn)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentEvent:
    kind: str
    text: str = ""
    completed: int = 0
    total: int = 0
    level: float | None = None


def emit(observer, kind, **data):
    if observer is not None:
        try:
            observer(AgentEvent(kind, **data))
        except Exception:
            # Graphics and other observers must never affect persistence or tools.
            pass
