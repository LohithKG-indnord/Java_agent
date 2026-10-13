"""Language routing for the shared Streamlit code-agent pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class LanguageSpec:
    key: str
    label: str
    syntax: str
    file_extension: str


LANGUAGE_SPECS = {
    "java": LanguageSpec("java", "Java", "java", ".java"),
    "node": LanguageSpec("node", "Node.js", "javascript", ".js"),
}


def get_language_spec(language: str) -> LanguageSpec:
    """Return a configured language or raise a clear routing error."""
    key = str(language).strip().lower()
    aliases = {"javascript": "node", "node.js": "node", "js": "node"}
    key = aliases.get(key, key)
    try:
        return LANGUAGE_SPECS[key]
    except KeyError as error:
        raise ValueError(f"Unsupported language: {language}") from error


def route_generation(
    language: str,
    question: str,
    handlers: dict[str, Callable[[str, Optional[object]], tuple[Optional[str], str]]],
    latency: Optional[object] = None,
) -> tuple[Optional[str], str]:
    """Dispatch generation to the selected language-specific handler."""
    spec = get_language_spec(language)
    try:
        handler = handlers[spec.key]
    except KeyError as error:
        raise ValueError(f"No generation handler registered for {spec.label}") from error
    return handler(question, latency)
