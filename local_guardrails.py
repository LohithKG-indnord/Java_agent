"""Optional local model guardrails.

These models run before Claude and do not consume Anthropic tokens:
* Sentence Transformers: semantic Java-scope classifier using calibrated prototypes.
* Meta Prompt Guard: local prompt-injection/jailbreak classifier.

The application can be configured to fail closed when these models are unavailable.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any


JAVA_PROTOTYPES = [
    "Write Java code to implement inheritance, polymorphism, and method overriding.",
    "Create a Java class using interfaces, constructors, and exception handling.",
    "How do I use ArrayList, HashMap, generics, and Java streams?",
    "Connect a Java application to MySQL using JDBC.",
    "Debug a JVM application or a Spring Boot service.",
    "Implement a Java multithreading or concurrency example.",
    "Write a JUnit test for a Java class.",
    "Explain Java bytecode, the JVM, or garbage collection.",
    "Create a REST API endpoint with request schema validation.",
    "Implement a backend service, controller, or JSON validation endpoint.",
]

NON_JAVA_PROTOTYPES = [
    "Write a Python web scraper or Django application.",
    "Create a JavaScript, TypeScript, or Node.js application.",
    "Build a React frontend using HTML and CSS.",
    "Write a C++, C#, Rust, Go, PHP, Ruby, or Kotlin program.",
    "Explain finance, travel, weather, cooking, or general knowledge.",
    "Write a shell script or PowerShell command.",
]


class LocalGuardrailUnavailable(RuntimeError):
    """Raised when strict local model validation cannot run."""


@lru_cache(maxsize=1)
def _load_semantic_model() -> tuple[Any, Any, Any]:
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError as error:
        raise LocalGuardrailUnavailable(
            "Install sentence-transformers to enable the local semantic Java guardrail."
        ) from error
    model_name = os.getenv("JAVA_SCOPE_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    model = SentenceTransformer(model_name)
    java_embeddings = model.encode(JAVA_PROTOTYPES, normalize_embeddings=True, convert_to_numpy=True)
    non_java_embeddings = model.encode(NON_JAVA_PROTOTYPES, normalize_embeddings=True, convert_to_numpy=True)
    return model, np.asarray(java_embeddings), np.asarray(non_java_embeddings)


@lru_cache(maxsize=1)
def _load_prompt_guard() -> Any:
    try:
        from transformers import pipeline
    except ImportError as error:
        raise LocalGuardrailUnavailable(
            "Install transformers to enable the local prompt-injection guardrail."
        ) from error
    model_name = os.getenv(
        "PROMPT_GUARD_MODEL", "protectai/deberta-v3-base-prompt-injection-v2"
    )
    return pipeline("text-classification", model=model_name, top_k=None)


def semantic_java_scope_check(text: str) -> tuple[bool, str]:
    """Accept only when Java similarity clears both a score and margin threshold."""
    model, java_embeddings, non_java_embeddings = _load_semantic_model()
    query = model.encode([text], normalize_embeddings=True, convert_to_numpy=True)[0]
    java_score = float((java_embeddings @ query).max())
    non_java_score = float((non_java_embeddings @ query).max())
    margin = java_score - non_java_score
    min_score = float(os.getenv("JAVA_SCOPE_MIN_SCORE", "0.55"))
    min_margin = float(os.getenv("JAVA_SCOPE_MIN_MARGIN", "0.06"))
    if java_score < min_score or margin < min_margin:
        return False, "The local semantic classifier could not verify that the request is Java-related."
    return True, ""


def prompt_injection_check(text: str) -> tuple[bool, str]:
    """Reject high-confidence injection/jailbreak classifications."""
    classifier = _load_prompt_guard()
    raw = classifier(text)
    scores = raw[0] if raw and isinstance(raw[0], list) else raw
    risk_score = 0.0
    recognized = False
    for item in scores:
        label = str(item.get("label", "")).lower()
        is_risk_label = (
            "injection" in label
            or "jailbreak" in label
            or label in {"label_1", "1", "unsafe", "malicious"}
        )
        if is_risk_label:
            recognized = True
            risk_score = max(risk_score, float(item.get("score", 0.0)))
        elif label in {"label_0", "0", "benign", "safe"}:
            recognized = True
    if not recognized:
        raise LocalGuardrailUnavailable("Prompt Guard returned an unsupported label set.")
    threshold = float(os.getenv("PROMPT_GUARD_THRESHOLD", "0.70"))
    if risk_score >= threshold:
        return False, "The local security classifier detected a prompt injection or jailbreak attempt."
    return True, ""


def check_local_models(text: str, require_semantic: bool = True) -> tuple[bool, str]:
    """Run local classifiers; semantic scope is optional for clear Java input."""
    allowed, reason = prompt_injection_check(text)
    if not allowed:
        return False, reason
    if not require_semantic:
        return True, ""
    return semantic_java_scope_check(text)
