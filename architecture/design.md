# Design: Java and Node.js Code Agent

## Overview

The application is a single-process Streamlit service. `app.py` owns the user interface, shared deterministic guardrails, Claude integration, logging, and latency reporting. The `agents/` package contains routing, shared helpers, and the Java or Node.js language pipelines. `local_guardrails.py` contains optional local model checks that run before Claude and do not consume Anthropic API tokens.

## Repository structure

```text
javaagentcodex/
├── architecture/
│   ├── design.md
│   └── proposal.md
├── app.py                    # Streamlit UI and application pipeline
├── agents/                   # Language agents and shared pipeline helpers
│   ├── router.py             # Java/Node.js language routing
│   ├── agent_common.py       # Shared source and compiler helpers
│   ├── java_agent.py         # Java prompt, javac validation, and repair
│   └── node_agent.py         # Node.js prompt, syntax validation, and repair
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

`main()` configures the page, renders the conversation history, lets the user select Java or Node.js, accepts a chat input, and displays validated source with language syntax highlighting. The sidebar shows only system-prompt, latest input, and latest output token counts. It does not display API cost, cache usage, or request totals.

Token counts use `tiktoken` and the `TOKENIZER_ENCODING` environment setting, defaulting to `o200k_base`. The system prompt is counted separately from the user input. These local counts are intended for visibility and are not provider billing measurements.

### Deterministic input guardrail

`input_guardrail()` normalizes Unicode and whitespace, then applies shared checks for:

- empty and oversized input;
- prompt-injection phrases;
- unsafe requests involving credential theft, malware, or unauthorized access;
- control characters;
- obvious cross-language requests;
- selected-language and programming intent signals.

The guardrail returns a `GuardrailResult` and does not call Claude.

### Optional local model guardrails

When `ENABLE_LOCAL_MODEL_GUARDRAILS=true`, `local_guardrails.py` runs:

1. a prompt-injection classifier using Transformers;
2. a semantic selected-language scope classifier using Sentence Transformers for ambiguous Java and Node.js requests. Java and Node.js use separate calibrated prototype sets and thresholds.

The models are cached in process. `REQUIRE_LOCAL_MODEL_GUARDRAILS=true` makes model-loading failures reject the request instead of silently continuing.

### Claude integration

`ask_claude()` reuses a cached Anthropic client and sends the question with the strict system prompt for the selected language. The system prompt is sent as a text content block with an Anthropic ephemeral `cache_control` breakpoint, allowing repeated requests with the same prompt prefix to reuse cached input tokens when the selected model's minimum cacheable-prefix requirement is met. The request asks for complete, small source without explanations or Markdown. Generation and repair budgets default to 4,096 tokens and can be changed with `MAX_GENERATION_TOKENS` and `MAX_REPAIR_TOKENS`.

At Streamlit startup, a cached background thread warms the tokenizer, Anthropic client, and enabled local guardrail models. The warm-up makes no API request and does not block the UI; a first request can still wait if a large local model has not finished loading.

### Output guardrail

`output_guardrail()` performs the following checks:

1. response exists and is under the configured character limit;
2. complete outer Markdown fences are removed;
3. trailing prose is trimmed when a complete Java type is found;
4. response size, Java type presence, prose, Markdown tables, and mixed-language patterns are checked;
5. delimiters are checked when compiler validation is not available;
6. selected process-control APIs are blocked;
7. Java source is compiled with `javac`; Node.js source is syntax-checked with `node --check`. npm is required as part of the Node.js toolchain but generated code is never installed or executed.

Compiler and syntax-check files are written to temporary directories and deleted after validation. `JAVA_GUARDRAIL_TEMP_DIR` and `NODE_GUARDRAIL_TEMP_DIR` can configure them; relative paths are resolved from the project directory. The generated source is never executed.

### Repair path

The selected language pipeline makes one additional Claude request only for selected retryable failures, including compiler/syntax rejection, explanation text, or missing source structure. The validation diagnostic is included in the repair request. If the second validation fails, the diagnostic is returned to the user.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Required Claude API credential |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Claude model name |
| `JAVA_RELEASE` | `17` | Java release passed to `javac` |
| `REQUIRE_JAVAC` | `true` | Fail closed when compiler validation is unavailable |
| `JAVAC_PATH` | none | Optional explicit compiler path |
| `JAVA_GUARDRAIL_TEMP_DIR` | system temp | Compiler scratch directory |
| `REQUIRE_NODE` | `true` | Fail closed when Node.js validation is unavailable |
| `NODE_PATH` | none | Optional explicit Node.js executable |
| `NPM_PATH` | none | Optional explicit npm executable |
| `NODE_GUARDRAIL_TEMP_DIR` | system temp | Node.js validation scratch directory |
| `TOKENIZER_ENCODING` | `o200k_base` | `tiktoken` encoding for visible token counters |
| `ENABLE_LOCAL_MODEL_GUARDRAILS` | `false` | Enable local model checks |
| `REQUIRE_LOCAL_MODEL_GUARDRAILS` | `true` | Reject if enabled local models cannot run |
| `JAVA_SCOPE_MIN_SCORE` | `0.55` | Minimum semantic Java similarity |
| `JAVA_SCOPE_MIN_MARGIN` | `0.06` | Required Java-vs-non-Java similarity margin |
| `NODE_SCOPE_MIN_SCORE` | `0.55` | Minimum semantic Node.js similarity |
| `NODE_SCOPE_MIN_MARGIN` | `0.06` | Required Node.js-vs-non-Node.js similarity margin |
| `SCOPE_MODEL` | Java model fallback | Shared Sentence Transformers model name |
| `PROMPT_GUARD_THRESHOLD` | `0.70` | Local injection-risk threshold |

## Observability

`agent.log` stores structured events for rejected inputs, API errors, compiler failures, repair attempts, and successful requests. It does not store API keys or full prompts. The log rotates at approximately 2 MB.

`latency_metrics.json` is overwritten after each request and records timings for input guardrails, local models, Claude calls, output validation, repair, and the selected Java or Node.js validator when those stages run.

## Testing strategy

The current test suite focuses on the deterministic guardrails:

- reject cross-language, unsafe, injection, and mixed-language inputs;
- accept valid Java and Node.js questions;
- accept compilable Java and syntactically valid Node.js source;
- reject prose, Markdown tables, and unbalanced output.

Run the tests with:

```powershell
python -m pytest -q
```

Tests set `REQUIRE_JAVAC=false` by default so they can run without a local JDK. Production-like validation should be exercised separately with JDK 17+ installed.

## Design constraints

- Language-specific behavior is routed through `agents/router.py` while shared safety and API behavior stay centralized.
- Claude is the only remote model call in the default path.
- Generated code is validated but never executed.
- API keys and `.env` files remain local and are excluded from version control.
- The default compiler requirement means an accessible JDK 17+ installation, or an explicit `JAVAC_PATH`, is part of the runtime prerequisite.
- `REQUIRE_JAVAC=false` is available for local experimentation when compiler access is unavailable.
- Node.js validation requires accessible `node` and `npm` executables, or explicit `NODE_PATH` and `NPM_PATH` values.
