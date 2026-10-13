"""Streamlit Java and Node.js coding assistant.

Pipeline:
    language router -> shared input guardrail -> Anthropic call
    -> language output guardrail -> optional repair call

The shared safety guardrails are intentionally local and deterministic so rejected
inputs do not spend tokens and model output cannot be displayed as prose.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv
import streamlit as st

load_dotenv()

from agents.router import LANGUAGE_SPECS, get_language_spec, route_generation
from agents.java_agent import (
    SYSTEM_PROMPT,
    find_javac,
    output_guardrail as _java_output_guardrail,
    generate_valid_java as _generate_valid_java,
)
from agents.node_agent import (
    NODE_SYSTEM_PROMPT,
    find_node,
    output_guardrail_node as _node_output_guardrail,
    generate_valid_node as _generate_valid_node,
)

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_INPUT_CHARS = 8_000
MAX_OUTPUT_CHARS = 20_000
MAX_OUTPUT_LINES = 300
LATENCY_REPORT_PATH = os.path.join(os.path.dirname(__file__), "latency_metrics.json")
LOG_PATH = os.path.join(os.path.dirname(__file__), "agent.log")

logger = logging.getLogger("java_code_agent")
if not logger.handlers:
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_event(event: str, **details: object) -> None:
    """Write structured diagnostics without logging prompts or secrets."""
    logger.info("%s | %s", event, json.dumps(details, default=str, sort_keys=True))
JAVA_RELEASE = os.getenv("JAVA_RELEASE", "17")
REQUIRE_NODE = os.getenv("REQUIRE_NODE", "true").lower() not in {"0", "false", "no"}
REQUIRE_JAVAC = os.getenv("REQUIRE_JAVAC", "true").lower() not in {"0", "false", "no"}
ENABLE_LOCAL_MODEL_GUARDRAILS = os.getenv("ENABLE_LOCAL_MODEL_GUARDRAILS", "false").lower() in {"1", "true", "yes"}
REQUIRE_LOCAL_MODEL_GUARDRAILS = os.getenv("REQUIRE_LOCAL_MODEL_GUARDRAILS", "true").lower() not in {"0", "false", "no"}
TOKENIZER_ENCODING = os.getenv("TOKENIZER_ENCODING", "o200k_base").strip() or "o200k_base"
MAX_GENERATION_TOKENS = int(os.getenv("MAX_GENERATION_TOKENS", "4096"))
MAX_REPAIR_TOKENS = int(os.getenv("MAX_REPAIR_TOKENS", "4096"))


class LatencyTracker:
    """Collect request timings and overwrite a readable JSON report."""

    def __init__(self) -> None:
        self.started_at = time.perf_counter()
        self.sections: dict[str, float] = {}

    def record(self, name: str, started_at: float) -> None:
        self.sections[name] = round(time.perf_counter() - started_at, 4)

    def report(self) -> dict[str, object]:
        return {
            "total_seconds": round(time.perf_counter() - self.started_at, 4),
            "sections_seconds": self.sections,
        }


@lru_cache(maxsize=4)
def get_tokenizer(encoding_name: str):
    """Load the configured local tokenizer once per encoding."""
    import tiktoken

    return tiktoken.get_encoding(encoding_name)


def count_tokens(text: str) -> int:
    """Count tokens locally using the configured tiktoken encoding."""
    return len(get_tokenizer(TOKENIZER_ENCODING).encode(text, disallowed_special=()))


def write_latency_report(tracker: LatencyTracker) -> None:
    report = tracker.report()
    with open(LATENCY_REPORT_PATH, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")

GENERATION_LOADER_HTML = """
<style>
.java-generation-loader {
    display: flex;
    align-items: center;
    gap: .65rem;
    min-height: 2rem;
    color: #6b7280;
    font-size: .95rem;
}
.java-generation-dots {
    display: inline-flex;
    gap: .22rem;
}
.java-generation-dots i {
    width: .38rem;
    height: .38rem;
    border-radius: 50%;
    background: #9ca3af;
    animation: javaDotPulse 1.2s ease-in-out infinite;
}
.java-generation-dots i:nth-child(2) { animation-delay: .16s; }
.java-generation-dots i:nth-child(3) { animation-delay: .32s; }
.java-generation-messages {
    position: relative;
    width: 17rem;
    height: 1.35rem;
}
.java-generation-messages span {
    position: absolute;
    inset: 0;
    opacity: 0;
    animation: javaMessageFade 18s ease-in-out infinite;
}
.java-generation-messages span:nth-child(2) { animation-delay: 3s; }
.java-generation-messages span:nth-child(3) { animation-delay: 6s; }
.java-generation-messages span:nth-child(4) { animation-delay: 9s; }
.java-generation-messages span:nth-child(5) { animation-delay: 12s; }
.java-generation-messages span:nth-child(6) { animation-delay: 15s; }
@keyframes javaDotPulse {
    0%, 60%, 100% { opacity: .35; transform: translateY(0); }
    30% { opacity: 1; transform: translateY(-2px); }
}
@keyframes javaMessageFade {
    0%, 100% { opacity: 0; transform: translateY(3px); }
    4%, 13% { opacity: 1; transform: translateY(0); }
    18% { opacity: 0; transform: translateY(-3px); }
}
@media (prefers-reduced-motion: reduce) {
    .java-generation-dots i, .java-generation-messages span { animation: none; }
    .java-generation-messages span { opacity: 0; }
    .java-generation-messages span:first-child { opacity: 1; }
}
</style>
<div class="java-generation-loader" role="status" aria-live="polite">
    <span class="java-generation-dots"><i></i><i></i><i></i></span>
    <span class="java-generation-messages">
        <span>Reading between the lines of your request</span>
        <span>Sketching a clean Java solution</span>
        <span>Giving the classes their proper places</span>
        <span>Making the types play nicely together</span>
        <span>Asking the compiler for a second opinion</span>
        <span>Polishing the final cup of code</span>
    </span>
</div>
"""


@dataclass
class GuardrailResult:
    allowed: bool
    reason: str = ""


# Signals are deliberately broad enough to support real Java questions while
# requiring a Java signal for generic programming terms such as "class".
JAVA_SIGNALS = {
    "java", "jdk", "jre", "jvm", "javac", "maven", "gradle", "spring boot",
    "spring", "hibernate", "junit", "jackson", "lombok", "android java",
    "servlet", "swing", "javafx", "jar", "pom.xml", "gradlew",
}
JAVA_CONCEPTS = {
    "inheritance", "polymorphism", "encapsulation", "abstraction", "constructor",
    "interface", "abstract class", "overloading", "overriding", "generic",
    "generics", "arraylist", "hashmap", "hashset", "linkedlist", "stream api",
    "lambda", "optional", "exception handling", "garbage collection", "reflection",
    "serialization", "deserialization", "multithreading", "concurrency", "jdbc",
    "type hierarchy", "child object", "parent object", "parent class", "child class",
    "subclass", "superclass", "inherits behavior", "method override", "method overriding",
    "quicksort", "quick sort", "mergesort", "merge sort", "heapsort", "heap sort",
    "binary search", "sorting", "recursion", "data structure", "data structures",
    "endpoint", "rest api", "schema verification", "request validation", "response validation",
}
JAVA_SYNTAX = re.compile(
    r"\b(public|private|protected|static|final|abstract|synchronized)\s+"
    r"(?:class|interface|enum|record|void|int|long|boolean|String)\b"
    r"|System\.out\.|import\s+(?:java|javax)\.|@(?:Override|Test|SpringBootApplication)\b",
    re.IGNORECASE,
)
PROGRAMMING_SIGNALS = {
    "code", "program", "programming", "class", "method", "function", "compile",
    "debug", "exception", "api", "thread", "list", "map", "array", "algorithm",
    "connect", "connection", "database", "query", "driver", "jdbc",
}
AMBIGUOUS_SOFTWARE_SIGNALS = {
    "api", "endpoint", "rest", "rest api", "web service", "service", "microservice", "schema",
    "schema validation", "request validation", "response validation", "json",
    "backend", "server", "controller", "route", "routing", "authentication",
    "authorization", "repository", "service layer", "webhook",
}
NODE_SIGNALS = {
    "node", "node.js", "nodejs", "javascript", "js", "npm", "npx", "express",
    "fastify", "nestjs", "node http", "node server", "package.json", "commonjs",
    "esm", "require", "module.exports",
}
NON_JAVA_SIGNALS = {
    "python", "javascript", "typescript", "node.js", "nodejs", "c++", "c#",
    "golang", "rust", "php", "ruby", "kotlin", "swift", "sql", "html", "css",
    "bash", "powershell", "matlab", "r language",
}
# Catch common spelling, spacing, and punctuation variants that would otherwise
# let a non-Java request pass because the language name is not one exact token.
NON_JAVA_PATTERNS = (
    r"\bjava[\s_-]*script\b",
    r"\btype[\s_-]*script\b",
    r"\bnode[\s._-]*js\b",
    r"\b(?:c[\s_-]*plus[\s_-]*plus|c[\s_-]*sharp)\b",
    r"\bobjective[\s_-]*c\b",
    r"\bvisual[\s_-]*basic\b",
    r"\b(?:vb[\s_-]*net|dot[\s_-]*net)\b",
    r"\bgo[\s_-]*lang\b",
    r"\bpower[\s_-]*shell\b",
    r"\b(?:ecma[\s_-]*script|js)\b",
    r"\b(?:scala|groovy|dart|perl|lua)\b",
)
PROMPT_INJECTION_PATTERNS = (
    r"ignore\s+(?:all|any|the|your|previous|prior)\s+instructions?",
    r"disregard\s+(?:all|any|the|your|previous|prior)",
    r"reveal\s+(?:the\s+)?(?:system|developer)\s+prompt",
    r"show\s+(?:the\s+)?(?:system|developer)\s+message",
    r"bypass\s+(?:the\s+)?(?:guardrail|filter|restriction)",
    r"jailbreak|prompt\s+injection|developer\s+message",
)
UNSAFE_REQUEST_PATTERNS = (
    r"\b(?:keylogger|ransomware|reverse\s+shell|credential\s+steal(?:er|ing)|"
    r"password\s+steal(?:er|ing)|malware|botnet|ddos)\b",
    r"\b(?:exfiltrate|steal|dump)\s+(?:tokens?|passwords?|credentials?|secrets?)\b",
)
PROGRAMMING_INTENT = re.compile(
    r"\b(?:write|generate|create|build|implement|show|explain|debug|fix|convert|"
    r"compile|run|use|connect|define|design|model|handle|sort|parse|read|write)\b"
    r"|\bhow\s+(?:do|can|would)\b|\bwhat\s+is\b",
    re.IGNORECASE,
)


def contains_term(text: str, term: str) -> bool:
    """Match a word or phrase without treating Java as part of JavaScript."""
    return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE))


def normalize_user_text(text: str) -> str:
    """Normalize Unicode tricks and invisible characters before inspection."""
    normalized = unicodedata.normalize("NFKC", text).replace("\u200b", "")
    return " ".join(normalized.split())


def input_guardrail(
    question: str,
    latency: Optional[LatencyTracker] = None,
    language: str = "java",
) -> GuardrailResult:
    """Fail closed using shared safety checks and language-specific scope checks."""
    language_spec = get_language_spec(language)
    text = normalize_user_text(question)
    lowered = text.lower()
    if not text:
        return GuardrailResult(False, f"Please enter a {language_spec.label} programming question.")
    if len(text) > MAX_INPUT_CHARS:
        return GuardrailResult(False, f"Please keep the question under {MAX_INPUT_CHARS:,} characters.")
    if any(re.search(pattern, lowered) for pattern in PROMPT_INJECTION_PATTERNS):
        return GuardrailResult(False, "Prompt-injection instructions are not accepted.")
    if any(re.search(pattern, lowered) for pattern in UNSAFE_REQUEST_PATTERNS):
        return GuardrailResult(False, "Requests for credential theft, malware, or unauthorized access are not accepted.")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        return GuardrailResult(False, "The question contains unsupported control characters.")
    if language_spec.key == "java":
        if (
            any(contains_term(lowered, signal) for signal in NON_JAVA_SIGNALS)
            or any(re.search(pattern, lowered) for pattern in NON_JAVA_PATTERNS)
        ):
            return GuardrailResult(False, "I only accept Java-related programming questions.")
    elif any(contains_term(lowered, signal) for signal in JAVA_SIGNALS) or any(
        contains_term(lowered, signal) for signal in JAVA_CONCEPTS
    ):
        return GuardrailResult(False, "I only accept Node.js-related programming questions.")

    has_java_signal = (
        any(contains_term(lowered, signal) for signal in JAVA_SIGNALS)
        or any(contains_term(lowered, signal) for signal in JAVA_CONCEPTS)
        or bool(JAVA_SYNTAX.search(text))
    )
    has_programming_signal = (
        bool(PROGRAMMING_INTENT.search(text))
        or any(contains_term(lowered, signal) for signal in PROGRAMMING_SIGNALS)
        or bool(JAVA_SYNTAX.search(text))
    )
    has_ambiguous_software_signal = any(
        contains_term(lowered, signal) for signal in AMBIGUOUS_SOFTWARE_SIGNALS
    )
    has_node_signal = any(contains_term(lowered, signal) for signal in NODE_SIGNALS)
    has_selected_language_signal = has_java_signal if language_spec.key == "java" else has_node_signal
    # Topic-only prompts remain valid for Java. Node.js topics such as Express or
    # npm are accepted directly; generic software prompts use the selected mode.
    if not (has_selected_language_signal or has_ambiguous_software_signal):
        return GuardrailResult(False, f"I only accept {language_spec.label}-related programming questions.")

    # Fast path: rules reject obvious bad input first. Run the local models
    # only after that, and use semantic scope classification only for prompts
    # whose language is ambiguous. This saves latency without weakening the
    # injection check for accepted requests.
    if ENABLE_LOCAL_MODEL_GUARDRAILS:
        local_models_started_at = time.perf_counter()
        try:
            from local_guardrails import check_local_models

            local_allowed, local_reason = check_local_models(
                text,
                require_semantic=not (
                    has_java_signal if language_spec.key == "java" else has_node_signal
                ),
                language=language_spec.key,
            )
        except Exception as error:
            if REQUIRE_LOCAL_MODEL_GUARDRAILS:
                return GuardrailResult(False, f"Local security models are unavailable: {error}")
        else:
            if not local_allowed:
                return GuardrailResult(False, local_reason)
        finally:
            if latency is not None:
                latency.record("local_model_guardrails", local_models_started_at)
    return GuardrailResult(True)


def remove_markdown_fences(text: str) -> str:
    """Remove accidental Markdown fences without changing source code."""
    cleaned = text.strip().replace("\r\n", "\n")
    cleaned = re.sub(
        r"^\s*```(?:java|javascript|js|node)?\s*\n", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"\n\s*```\s*$", "", cleaned)
    return cleaned.strip()


def extract_java_source(text: str) -> str:
    """Keep only a Java source candidate; validation decides whether it is safe."""
    cleaned = remove_markdown_fences(text)
    start_match = re.search(
        r"(?m)^\s*(?:package\s+[\w.]+\s*;|import\s+[\w.*]+\s*;|"
        r"(?:(?:public|private|protected|abstract|final|static)\s+)*"
        r"(?:class|interface|enum|record)\s+[A-Za-z_$][\w$]*)",
        cleaned,
    )
    if not start_match:
        return cleaned.strip()
    source = cleaned[start_match.start():].strip()

    # Drop trailing prose after the last top-level Java type while preserving
    # braces inside strings and comments for the later balance check.
    masked = re.sub(
        r'""".*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*.*?\*/',
        lambda match: " " * len(match.group(0)),
        source,
        flags=re.DOTALL,
    )
    depth = 0
    last_complete_type = None
    for index, char in enumerate(masked):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                last_complete_type = index + 1
            elif depth < 0:
                break
    if last_complete_type is not None:
        source = source[:last_complete_type]
    return source.strip()


def extract_node_source(text: str) -> str:
    """Keep a Node.js source candidate and trim trailing explanation text."""
    cleaned = remove_markdown_fences(text)
    start_match = re.search(
        r"(?m)^\s*(?:#!.*|(?:import|export)\s+|(?:const|let|var|function|class)\s+|"
        r"(?:require\(|module\.exports|console\.)|\s*\(async\s*\()",
        cleaned,
    )
    if start_match:
        cleaned = cleaned[start_match.start() :].strip()
    return cleaned


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


def contains_non_java_prose(source: str) -> bool:
    """Catch common model explanations that are not Java source."""
    first_line = next((line.strip() for line in source.splitlines() if line.strip()), "")
    prose_starts = ("here is", "sure", "of course", "this code", "explanation:", "solution:")
    markdown_table = any(re.match(r"^\s*\|.*\|\s*$", line) for line in source.splitlines())
    markdown_formatting = "**" in source or "`" in source
    return (
        first_line.lower().startswith(prose_starts)
        or markdown_table
        or markdown_formatting
    )


def contains_non_node_prose(source: str) -> bool:
    """Catch explanations without rejecting valid JavaScript template literals."""
    first_line = next((line.strip() for line in source.splitlines() if line.strip()), "")
    prose_starts = ("here is", "sure", "of course", "this code", "explanation:", "solution:")
    markdown_table = any(re.match(r"^\s*\|.*\|\s*$", line) for line in source.splitlines())
    return first_line.lower().startswith(prose_starts) or markdown_table or "**" in source


def find_javac() -> Optional[str]:
    """Use an explicitly configured portable JDK before searching PATH."""
    configured = os.getenv("JAVAC_PATH", "").strip().strip('"')
    if configured and os.path.isfile(configured):
        return configured
    return shutil.which("javac")


def find_node() -> Optional[str]:
    """Use an explicitly configured Node.js runtime before searching PATH."""
    configured = os.getenv("NODE_PATH", "").strip().strip('"')
    if configured and os.path.isfile(configured):
        return configured
    return shutil.which("node")


def find_npm() -> Optional[str]:
    """Locate npm for Node.js toolchain diagnostics."""
    configured = os.getenv("NPM_PATH", "").strip().strip('"')
    if configured and os.path.isfile(configured):
        return configured
    return shutil.which("npm") or shutil.which("npm.cmd")


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


def compile_java(source: str, latency: Optional[LatencyTracker] = None) -> tuple[bool, str]:
    """Compile source with javac when available; return (passed, diagnostic)."""
    javac = find_javac()
    if not javac:
        if REQUIRE_JAVAC:
            return False, "javac is required for output validation. Install JDK 17+ and add javac to PATH."
        return True, "javac not installed; structural validation used because REQUIRE_JAVAC=false."
    # javac requires the file name to match the public top-level type. Prefer
    # that declaration over a preceding package-private helper class.
    class_match = re.search(
        r"\bpublic\s+(?:final\s+|abstract\s+)?(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)",
        source,
    )
    if not class_match:
        class_match = re.search(
            r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)", source
        )
    class_name = class_match.group(1) if class_match else "Solution"
    # Keep compiler scratch files outside the project directory.  This avoids
    # OneDrive sync/locking issues while preserving an explicit override for
    # deployments that need a dedicated temporary location.
    configured_temp_root = os.getenv("JAVA_GUARDRAIL_TEMP_DIR", "").strip()
    temp_root = configured_temp_root or tempfile.gettempdir()
    if not os.path.isabs(temp_root):
        temp_root = os.path.join(os.path.dirname(__file__), temp_root)
    try:
        os.makedirs(temp_root, exist_ok=True)
        folder = tempfile.mkdtemp(prefix=".java_guardrail_", dir=temp_root)
    except OSError:
        return False, "Compiler validation could not create its temporary folder. Check project-folder permissions."
    try:
        package_match = re.search(r"^\s*package\s+([\w.]+)\s*;", source, re.MULTILINE)
        package_dir = os.path.join(folder, *(package_match.group(1).split("."))) if package_match else folder
        try:
            os.makedirs(package_dir, exist_ok=True)
        except OSError:
            if REQUIRE_JAVAC:
                return False, "Compiler validation could not create its temporary package folder."
            return True, "Compiler validation unavailable; structural validation used because REQUIRE_JAVAC=false."
        java_file = os.path.join(package_dir, f"{class_name}.java")
        try:
            with open(java_file, "w", encoding="utf-8") as handle:
                handle.write(source)
        except OSError:
            if REQUIRE_JAVAC:
                return False, "Compiler validation could not create its temporary source file. Check project-folder permissions."
            return True, "Compiler validation unavailable; structural validation used because REQUIRE_JAVAC=false."
        javac_started_at = time.perf_counter()
        try:
            result = subprocess.run(
                [javac, "--release", JAVA_RELEASE, "-Xlint:all", "-proc:none", java_file],
                capture_output=True,
                text=True,
                timeout=12,
            )
        except subprocess.TimeoutExpired:
            return False, "javac validation timed out."
        finally:
            if latency is not None:
                latency.record("javac_compile", javac_started_at)
        if result.returncode:
            diagnostic = (result.stderr or result.stdout).strip().splitlines()
            return False, "javac rejected the generated code: " + (diagnostic[0] if diagnostic else "syntax error")
        return True, "Compiled successfully with javac."
    finally:
        cleanup_compiler_folder(folder)


def compile_node(source: str, latency: Optional[LatencyTracker] = None) -> tuple[bool, str]:
    """Validate Node.js syntax with node --check; npm is reported when absent."""
    node = find_node()
    if not node:
        if REQUIRE_NODE:
            return False, "Node.js is required for output validation. Install Node.js and add node to PATH."
        return True, "Node.js not installed; structural validation used because REQUIRE_NODE=false."
    if not find_npm():
        if REQUIRE_NODE:
            return False, "npm is required for Node.js validation. Install npm with Node.js and add it to PATH."
        return True, "npm not installed; structural validation used because REQUIRE_NODE=false."
    temp_root = os.getenv("NODE_GUARDRAIL_TEMP_DIR", "").strip() or tempfile.gettempdir()
    if not os.path.isabs(temp_root):
        temp_root = os.path.join(os.path.dirname(__file__), temp_root)
    try:
        os.makedirs(temp_root, exist_ok=True)
        folder = tempfile.mkdtemp(prefix=".node_guardrail_", dir=temp_root)
    except OSError:
        return False, "Node.js validation could not create its temporary folder. Check permissions."
    try:
        source_file = os.path.join(folder, "solution.js")
        try:
            with open(source_file, "w", encoding="utf-8") as handle:
                handle.write(source)
        except OSError:
            return False, "Node.js validation could not create its temporary source file."
        node_started_at = time.perf_counter()
        try:
            result = subprocess.run(
                [node, "--check", source_file],
                capture_output=True,
                text=True,
                timeout=12,
            )
        except subprocess.TimeoutExpired:
            return False, "Node.js syntax validation timed out."
        finally:
            if latency is not None:
                latency.record("node_compile", node_started_at)
        if result.returncode:
            diagnostic = (result.stderr or result.stdout).strip().splitlines()
            return False, "Node.js rejected the generated code: " + (diagnostic[0] if diagnostic else "syntax error")
        return True, "Validated successfully with node --check."
    finally:
        cleanup_compiler_folder(folder)


def output_guardrail(
    raw_output: str, latency: Optional[LatencyTracker] = None
) -> tuple[Optional[str], str]:
    """Fail closed unless the complete response is source that javac accepts."""
    if not raw_output or len(raw_output) > MAX_OUTPUT_CHARS:
        return None, "The model returned no code or an oversized response. Please try a smaller request."
    # A complete outer fence is harmless presentation markup: remove it before
    # validation so users do not get rejected solely because Claude added one.
    # Any prose, embedded fence, or partial fence remains rejected below.
    source = extract_java_source(raw_output)
    if len(source.splitlines()) > MAX_OUTPUT_LINES:
        return None, "The generated source is too large. Please request a smaller focused example."
    if contains_non_java_prose(source):
        return None, "The model returned explanation text instead of Java code. Please try again."
    if not re.search(r"\b(class|interface|enum|record)\s+[A-Za-z_$][\w$]*", source):
        return None, "The response did not contain a Java type. Please try again."
    # When javac is available, let the real Java parser decide. The heuristic
    # delimiter scan is only a fallback for environments without a compiler.
    if not balanced_delimiters(source) and not (REQUIRE_JAVAC and find_javac()):
        return None, "The generated Java code has unbalanced delimiters. Please try again."
    if re.search(r"(^|\n)\s*(import\s+python|def\s+\w+|function\s+\w+|const\s+\w+)", source, re.IGNORECASE):
        return None, "The response contained non-Java code. Please try again."
    if re.search(r"\b(?:Runtime\.getRuntime|ProcessBuilder|System\.exit)\b", source):
        return None, "The generated code contains a blocked process-control API. Please try again."
    passed, diagnostic = compile_java(source, latency=latency)
    if not passed:
        return None, diagnostic
    return source, diagnostic


def output_guardrail_node(
    raw_output: str, latency: Optional[LatencyTracker] = None
) -> tuple[Optional[str], str]:
    """Validate a complete Node.js source response without executing it."""
    if not raw_output or len(raw_output) > MAX_OUTPUT_CHARS:
        return None, "The model returned no code or an oversized response. Please try a smaller request."
    source = extract_node_source(raw_output)
    if len(source.splitlines()) > MAX_OUTPUT_LINES:
        return None, "The generated source is too large. Please request a smaller focused example."
    if contains_non_node_prose(source):
        return None, "The model returned explanation text instead of Node.js code. Please try again."
    if not re.search(
        r"\b(?:const|let|var|function|class|import|export)\b|\brequire\s*\(|"
        r"\bmodule\.exports\b|\bconsole\.",
        source,
    ):
        return None, "The response did not contain recognizable Node.js code. Please try again."
    if not balanced_delimiters(source) and not (REQUIRE_NODE and find_node()):
        return None, "The generated Node.js code has unbalanced delimiters. Please try again."
    if re.search(r"(^|\n)\s*(?:public\s+class|import\s+java|def\s+\w+)", source, re.IGNORECASE):
        return None, "The response contained non-Node.js code. Please try again."
    if re.search(r"\b(?:child_process|process\.exit|execSync|spawnSync)\b", source):
        return None, "The generated code contains a blocked process-control API. Please try again."
    passed, diagnostic = compile_node(source, latency=latency)
    if not passed:
        return None, diagnostic
    return source, diagnostic


@lru_cache(maxsize=2)
def get_anthropic_client(api_key: str) -> Anthropic:
    """Reuse the API client across Streamlit reruns for this credential."""
    return Anthropic(api_key=api_key)


def _warmup_resources() -> None:
    """Warm local resources in the background without making an API request."""
    try:
        count_tokens(SYSTEM_PROMPT)
        count_tokens(NODE_SYSTEM_PROMPT)
    except Exception as error:
        log_event("warmup_failed", resource="tokenizer", error_type=type(error).__name__)

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        try:
            get_anthropic_client(api_key)
        except Exception as error:
            log_event("warmup_failed", resource="anthropic_client", error_type=type(error).__name__)

    if ENABLE_LOCAL_MODEL_GUARDRAILS:
        try:
            from local_guardrails import _load_prompt_guard, _load_semantic_model

            _load_prompt_guard()
            _load_semantic_model()
        except Exception as error:
            # The normal request path still applies REQUIRE_LOCAL_MODEL_GUARDRAILS.
            log_event("warmup_failed", resource="local_guardrails", error_type=type(error).__name__)


@st.cache_resource(show_spinner=False)
def start_background_warmup() -> bool:
    """Start one process-level warm-up thread and return immediately."""
    threading.Thread(target=_warmup_resources, name="java-agent-warmup", daemon=True).start()
    return True


def ask_claude(
    question: str,
    max_tokens: int = MAX_GENERATION_TOKENS,
    latency: Optional[LatencyTracker] = None,
    latency_name: str = "claude_api_call",
    system_prompt: str = SYSTEM_PROMPT,
) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        try:
            import streamlit as st

            api_key = str(st.secrets.get("ANTHROPIC_API_KEY", "")).strip()
        except Exception:
            pass
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is missing. Add it to a .env file, Streamlit secrets, "
            "or your terminal environment before starting Streamlit."
        )
    client = get_anthropic_client(api_key)
    started_at = time.perf_counter()
    try:
        message = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": question}],
        )
    except Exception as error:
        log_event(
            "claude_api_error",
            error_type=type(error).__name__,
            error=str(error),
            question_length=len(question),
            model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
        )
        raise
    finally:
        if latency is not None:
            latency.record(latency_name, started_at)
    return "\n".join(block.text for block in message.content if getattr(block, "type", "") == "text")


# Language-specific implementations live in the agents/ package.
# These small adapters keep the entry point responsible only for shared wiring.
output_guardrail = _java_output_guardrail
output_guardrail_node = _node_output_guardrail


def generate_valid_java(
    question: str,
    latency: Optional[LatencyTracker] = None,
) -> tuple[Optional[str], str]:
    return _generate_valid_java(question, ask_claude, MAX_REPAIR_TOKENS, latency, log_event)


def generate_valid_node(
    question: str,
    latency: Optional[LatencyTracker] = None,
) -> tuple[Optional[str], str]:
    return _generate_valid_node(question, ask_claude, MAX_REPAIR_TOKENS, latency, log_event)


def main() -> None:
    st.set_page_config(page_title="Code Agent", page_icon="💻", layout="centered")
    start_background_warmup()
    st.title("💻 Code Agent")

    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "token_counts" not in st.session_state:
        st.session_state.token_counts = {"input_tokens": 0, "output_tokens": 0}

    with st.sidebar:
        language_label = st.selectbox(
            "Language",
            options=[spec.label for spec in LANGUAGE_SPECS.values()],
            index=0,
        )
    language_spec = get_language_spec(language_label)
    system_prompt = SYSTEM_PROMPT if language_spec.key == "java" else NODE_SYSTEM_PROMPT

    with st.sidebar:
        st.metric("System prompt tokens", f"{count_tokens(system_prompt):,}")
        st.metric("Input tokens", f"{st.session_state.token_counts['input_tokens']:,}")
        st.metric("Output tokens", f"{st.session_state.token_counts['output_tokens']:,}")

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message["role"] == "assistant":
                st.code(message["content"], language=message.get("syntax", "java"))
            else:
                st.write(message["content"])

    question = st.chat_input(f"Write your {language_spec.label} prompt here ...")
    if not question:
        return

    log_event("request_received", question_length=len(question))
    st.session_state.messages.append(
        {"role": "user", "content": question, "language": language_spec.key}
    )
    with st.chat_message("user"):
        st.write(question)

    latency = LatencyTracker()
    input_started_at = time.perf_counter()
    input_check = input_guardrail(question, latency=latency, language=language_spec.key)
    latency.record("input_guardrail", input_started_at)
    if not input_check.allowed:
        log_event("input_rejected", reason=input_check.reason, question_length=len(question))
        with st.chat_message("assistant"):
            st.warning(input_check.reason)
        write_latency_report(latency)
        return

    compiler_available = find_javac() if language_spec.key == "java" else find_node()
    compiler_required = REQUIRE_JAVAC if language_spec.key == "java" else REQUIRE_NODE
    if compiler_required and not compiler_available:
        log_event(
            "compiler_unavailable",
            language=language_spec.key,
            compiler="javac" if language_spec.key == "java" else "node/npm",
            required=compiler_required,
        )
        with st.chat_message("assistant"):
            if language_spec.key == "java":
                st.error("Configuration error: JDK 17+ is required. Install javac and add it to PATH before using the agent.")
            else:
                st.error("Configuration error: Node.js is required. Install Node.js and add node to PATH before using the agent.")
        write_latency_report(latency)
        return

    with st.chat_message("assistant"):
        loader = st.empty()
        loader.markdown(GENERATION_LOADER_HTML, unsafe_allow_html=True)
        try:
            source, diagnostic = route_generation(
                language_spec.key,
                question,
                {"java": generate_valid_java, "node": generate_valid_node},
                latency=latency,
            )
            st.session_state.token_counts = {
                "input_tokens": count_tokens(question),
                "output_tokens": count_tokens(source or ""),
            }
        except Exception as error:  # Show safe, actionable UI error without exposing secrets.
            loader.empty()
            log_event(
                "generation_error",
                error_type=type(error).__name__,
                error=str(error),
                question_length=len(question),
            )
            st.error(f"Unable to generate code: {html.escape(str(error))}")
            write_latency_report(latency)
            return
        loader.empty()
        if source is None:
            log_event("output_validation_failed", diagnostic=diagnostic)
            st.error(diagnostic)
            write_latency_report(latency)
            return
        st.code(source, language=language_spec.syntax)
        with st.expander("Validation"):
            st.caption(diagnostic)
            st.caption("Stage timings")
            for stage, seconds in latency.report()["sections_seconds"].items():
                st.caption(f"{stage}: {seconds:.3f}s")
        st.session_state.messages.append(
            {"role": "assistant", "content": source, "language": language_spec.key, "syntax": language_spec.syntax}
        )
        log_event("generation_succeeded", source_length=len(source), latency=latency.report())
        write_latency_report(latency)
        # The sidebar rendered before generation started; redraw it with the
        # token counts calculated for this prompt and response.
        st.rerun()


if __name__ == "__main__":
    main()
