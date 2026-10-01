# Design: Java Code Agent

## Overview

The application is a single-process Streamlit service. `app.py` owns the user interface, request orchestration, deterministic guardrails, Claude integration, Java compilation, logging, and latency reporting. `local_guardrails.py` contains optional local model checks that run before Claude and do not consume Anthropic API tokens.

## Repository structure

```text
javaagentcodex/
├── architecture/
│   ├── design.md
│   └── proposal.md
├── app.py                    # Streamlit UI and application pipeline
├── local_guardrails.py       # Optional local semantic and injection checks
├── test_guardrails.py        # Guardrail unit tests
├── requirements.txt          # Runtime and local-model dependencies
├── .env.example              # Configuration template
├── agent.log                 # Rotating operational diagnostics
├── latency_metrics.json      # Latest request-stage timings
├── CHANGELOG.md
├── HANDOFF.md
└── README.md
```

## Runtime components

### Streamlit UI

`main()` configures the page, renders the conversation history, accepts a chat input, and displays validated Java source with Java syntax highlighting. The sidebar shows only system-prompt, latest input, and latest output token counts. It does not display API cost, cache usage, or request totals.

Token counts use `tiktoken` and the `TOKENIZER_ENCODING` environment setting, defaulting to `o200k_base`. The system prompt is counted separately from the user input. These local counts are intended for visibility and are not provider billing measurements.

### Deterministic input guardrail

`input_guardrail()` normalizes Unicode and whitespace, then applies local checks for:

- empty and oversized input;
- prompt-injection phrases;
- unsafe requests involving credential theft, malware, or unauthorized access;
- control characters;
- obvious non-Java languages and topics;
- Java and programming intent signals.

The guardrail returns a `GuardrailResult` and does not call Claude.

### Optional local model guardrails

When `ENABLE_LOCAL_MODEL_GUARDRAILS=true`, `local_guardrails.py` runs:

1. a prompt-injection classifier using Transformers;
2. a semantic Java-scope classifier using Sentence Transformers for ambiguous requests.

The models are cached in process. `REQUIRE_LOCAL_MODEL_GUARDRAILS=true` makes model-loading failures reject the request instead of silently continuing.

### Claude integration

`ask_claude()` reuses a cached Anthropic client and sends the question with a strict system prompt. The default model is configured by `ANTHROPIC_MODEL` and currently defaults to `claude-sonnet-4-6`. The request uses temperature `0` and asks for complete Java source without explanations or Markdown.

### Output guardrail

`output_guardrail()` performs the following checks:

1. response exists and is under the configured character limit;
2. complete outer Markdown fences are removed;
3. trailing prose is trimmed when a complete Java type is found;
4. response size, Java type presence, prose, Markdown tables, and mixed-language patterns are checked;
5. delimiters are checked when compiler validation is not available;
6. selected process-control APIs are blocked;
7. the source is compiled with `javac` using the configured Java release.

Compiler files are written to a temporary directory and deleted after validation. If `JAVA_GUARDRAIL_TEMP_DIR` is configured, the directory is created automatically; relative paths are resolved from the project directory. The generated source is never executed.

### Repair path

`generate_valid_java()` makes one additional Claude request only for selected retryable failures, including compiler rejection, explanation text, or a missing Java type. The compiler or validation diagnostic is included in the repair request. If the second validation fails, the diagnostic is returned to the user.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Required Claude API credential |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Claude model name |
| `JAVA_RELEASE` | `17` | Java release passed to `javac` |
| `REQUIRE_JAVAC` | `true` | Fail closed when compiler validation is unavailable |
| `JAVAC_PATH` | none | Optional explicit compiler path |
| `JAVA_GUARDRAIL_TEMP_DIR` | system temp | Compiler scratch directory |
| `TOKENIZER_ENCODING` | `o200k_base` | `tiktoken` encoding for visible token counters |
| `ENABLE_LOCAL_MODEL_GUARDRAILS` | `false` | Enable local model checks |
| `REQUIRE_LOCAL_MODEL_GUARDRAILS` | `true` | Reject if enabled local models cannot run |
| `JAVA_SCOPE_MIN_SCORE` | `0.55` | Minimum semantic Java similarity |
| `JAVA_SCOPE_MIN_MARGIN` | `0.06` | Required Java-vs-non-Java similarity margin |
| `PROMPT_GUARD_THRESHOLD` | `0.70` | Local injection-risk threshold |

## Observability

`agent.log` stores structured events for rejected inputs, API errors, compiler failures, repair attempts, and successful requests. It does not store API keys or full prompts. The log rotates at approximately 2 MB.

`latency_metrics.json` is overwritten after each request and records timings for input guardrails, local models, Claude calls, output validation, repair, and `javac` when those stages run.

## Testing strategy

The current test suite focuses on the deterministic guardrails:

- reject non-Java, unsafe, injection, and mixed-language inputs;
- accept valid Java questions and topic-only Java prompts;
- accept compilable Java source;
- reject prose, Markdown tables, and unbalanced output.

Run the tests with:

```powershell
python -m pytest -q
```

Tests set `REQUIRE_JAVAC=false` by default so they can run without a local JDK. Production-like validation should be exercised separately with JDK 17+ installed.

## Design constraints

- The application is intentionally single-file at its current scale.
- Claude is the only remote model call in the default path.
- Generated code is validated but never executed.
- API keys and `.env` files remain local and are excluded from version control.
- The default compiler requirement means an accessible JDK 17+ installation, or an explicit `JAVAC_PATH`, is part of the runtime prerequisite.
- `REQUIRE_JAVAC=false` is available for local experimentation when compiler access is unavailable.
