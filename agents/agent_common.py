"""Shared source-cleaning and validation helpers for language agents."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from typing import Any, Optional


MAX_OUTPUT_CHARS = 20_000
MAX_OUTPUT_LINES = 300


def remove_markdown_fences(text: str) -> str:
    """Remove accidental outer Markdown fences without changing source code."""
    cleaned = text.strip().replace("\r\n", "\n")
    cleaned = re.sub(
        r"^\s*```(?:java|javascript|js|node)?\s*\n", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"\n\s*```\s*$", "", cleaned)
    return cleaned.strip()


def balanced_delimiters(source: str) -> bool:
    """Check braces, brackets, and parentheses while ignoring strings/comments."""
    cleaned = re.sub(r'""".*?"""', '"""', source, flags=re.DOTALL)
    cleaned = re.sub(r'"(?:\\.|[^"\\])*"', '""', cleaned)
    cleaned = re.sub(r"'(?:\\.|[^'\\])*'", "''", cleaned)
    cleaned = re.sub(r"//.*|/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    pairs = {"}": "{", ")": "(", "]": "["}
    stack: list[str] = []
    for char in cleaned:
        if char in "{([":
            stack.append(char)
        elif char in "})]":
            if not stack or stack.pop() != pairs[char]:
                return False
    return not stack


def contains_prose(source: str, allow_backticks: bool = False) -> bool:
    """Catch common explanations while allowing JavaScript template literals."""
    first_line = next((line.strip() for line in source.splitlines() if line.strip()), "")
    prose_starts = ("here is", "sure", "of course", "this code", "explanation:", "solution:")
    markdown_table = any(re.match(r"^\s*\|.*\|\s*$", line) for line in source.splitlines())
    markdown_formatting = "**" in source or (not allow_backticks and "`" in source)
    return first_line.lower().startswith(prose_starts) or markdown_table or markdown_formatting


def cleanup_compiler_folder(folder: str) -> None:
    """Best-effort Windows cleanup, including read-only OneDrive files."""
    try:
        for root, directories, files in os.walk(folder, topdown=False):
            for name in files + directories:
                path = os.path.join(root, name)
                try:
                    os.chmod(path, stat.S_IWRITE)
                except OSError:
                    pass
        shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass


def compiler_temp_root(environment_name: str) -> str:
    """Resolve a language-specific compiler scratch directory."""
    configured = os.getenv(environment_name, "").strip()
    root = configured or tempfile.gettempdir()
    if not os.path.isabs(root):
        root = os.path.join(os.path.dirname(__file__), root)
    return root


def record_stage(latency: Optional[Any], name: str, started_at: float) -> None:
    if latency is not None:
        latency.record(name, started_at)
