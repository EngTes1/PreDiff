# PreDiff

PreDiff is an implementation-aware testing pipeline for Java refactoring
engines. It recovers refactoring preconditions from previously collected
method-level source snippets, compares the recovered preconditions across
engines, generates discrepancy-guided Java tests, and executes the same
refactoring requests in multiple IDEs.

This repository contains the executable pipeline code only. It does not
include the precondition corpus, IDE source repositories, generated tests,
experiment outputs, or IDE installations.

## Bug List

The complete list of submitted bugs is available here:

[View the interactive bug list](https://engtes1.github.io/PreDiff/bug-list.html)

## Pipeline

```text
Collected precondition snippets
  -> precondition recovery and normalization
  -> engine-level precondition profiles
  -> cross-engine precondition comparison
  -> discrepancy-guided test generation
  -> Eclipse / IntelliJ IDEA / NetBeans execution
  -> compilation, source-diff, and behavior records
```

## Repository Layout

```text
precondition_agent/             Core analysis, comparison, and generation code
tools/java_ast_helper/          JDK-based Java AST extraction helper
prompts/                        Optional test-generation guidance
refactoring_test_runner/        Test discovery, compilation, and IDE runners
eclipse_refactoring_backend/    Eclipse JDT/LTK backend
intellij_refactoring_backend/   IntelliJ IDEA backend
netbeans_refactoring_backend/   NetBeans backend
batch_analyze_corpus.py         Batch precondition-analysis entry point
compare_preconditions.py        Cross-engine comparison entry point
generate_tests_from_comparison.py
                                Test-generation entry point
```

## Prerequisites

- Python 3.10 or later.
- JDK 17 or later for precondition analysis; JDK 21 is recommended for the
  complete IDE-runner workflow.
- [ripgrep](https://github.com/BurntSushi/ripgrep) available as `rg`.
- An OpenAI-compatible LLM endpoint and API key.
- Local source checkouts of the Eclipse JDT UI, IntelliJ IDEA Community, and
  Apache NetBeans repositories.
- Local Eclipse, IntelliJ IDEA, and NetBeans installations for differential
  execution.

Install the Python dependency in a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`tree-sitter-language-pack` is optional. Install it only when Kotlin AST
support is required:

```powershell
python -m pip install tree-sitter-language-pack
```

## LLM Configuration

Do not put an API key in the source code. Configure it in the current shell:

```powershell
$env:DEEPSEEK_API_KEY = "<YOUR_API_KEY>"
$env:LLM_BASE_URL = "<OPENAI_COMPATIBLE_ENDPOINT>"
```

`OPENAI_API_KEY` can be used instead of `DEEPSEEK_API_KEY`. Command-line
options override environment variables, but passing a secret through
`--api-key` is discouraged because it may be retained in shell history.

The model identifiers below are the stage-specific identifiers used in the
experiment. They must be replaced if the configured endpoint uses different
model names.

| Stage | Model identifier | Temperature |
| --- | --- | ---: |
| Precondition analysis | `deepseek-flash` | 0.0 |
| Cross-engine comparison | `deepseek-v4-pro` | 0.0 |
| Test generation | `deepseek-v4-pro` | 0.8 |

The commands below explicitly pass model settings instead of relying on the
local development defaults embedded in the scripts.

## Input Corpus

PreDiff expects an already collected method-level precondition corpus. The
corpus can either be the directory shown below or a parent directory that
contains an inner `corpus` directory.

```text
<CORPUS_ROOT>/
  Eclipse/
    InlineMethod/
      required/
        <method-snippet files>
  IntelliJ IDEA/
    InlineMethod/
      required/
        <method-snippet files>
  netbeans/
    InlineMethod/
      required/
        <method-snippet files>
```

The main experiment analyzes the `required` phase. Snippet files placed
directly in a selected operation directory are also treated as `required`.
Use the following corpus-facing refactoring names:

| Operation | `--refactoring` value |
| --- | --- |
| Rename Method | `Rename/RenameMethod` |
| Rename Field | `Rename/RenameField` |
| Rename Variable | `Rename/RenameVariable` |
| Extract Method | `ExtractMethod` |
| Extract Variable | `ExtractVariable` |
| Move Instance Method | `MoveInstanceMethod` |
| Inline Method | `InlineMethod` |

When a refactoring has multiple implementation variants, use
`--implementation` together with an engine filter. For example, IntelliJ IDEA
Extract Method can be restricted to its `legacy` directory with `--engine
"IntelliJ IDEA" --implementation legacy`. The Eclipse and NetBeans Extract
Method inputs are analyzed separately without that implementation argument.

## 1. Recover Precondition Profiles

Run the analysis from the repository root. Always provide the corpus and
source-repository paths explicitly; the paths embedded in the scripts are
local development defaults.

```powershell
python batch_analyze_corpus.py `
  --corpus-root "<CORPUS_ROOT>" `
  --refactoring "InlineMethod" `
  --phase required `
  --repo-root "Eclipse=<ECLIPSE_JDT_UI_SOURCE>" `
  --repo-root "IntelliJ IDEA=<INTELLIJ_COMMUNITY_SOURCE>" `
  --repo-root "netbeans=<NETBEANS_SOURCE>" `
  --output-root "outputs\analysis" `
  --model "deepseek-flash" `
  --base-url "$env:LLM_BASE_URL" `
  --thinking-mode disabled `
  --reasoning-effort= `
  --temperature 0 `
  --trace-level compact `
  --resume
```

The batch produces one group per engine and refactoring:

```text
outputs/analysis/<Engine>/<Refactoring>/
  raw_analyses.json
  precondition_profile.json
  batch_errors.json
```

Use `--dry-run` to inspect corpus discovery without calling the LLM. The Java
AST helper is compiled automatically into `.ast_helper_build/` on first use.
Use `--disable-ast` only when AST retrieval is intentionally disabled.

## 2. Compare Engines

Pass one profile for each participating engine:

```powershell
python compare_preconditions.py `
  --profile "outputs\analysis\Eclipse\InlineMethod\precondition_profile.json" `
  --profile "outputs\analysis\IntelliJ IDEA\InlineMethod\precondition_profile.json" `
  --profile "outputs\analysis\netbeans\InlineMethod\precondition_profile.json" `
  --output "outputs\comparison\InlineMethod_comparison_report.json" `
  --model "deepseek-v4-pro" `
  --base-url "$env:LLM_BASE_URL" `
  --thinking-mode disabled `
  --reasoning-effort= `
  --temperature 0 `
  --trace-level compact
```

The comparison report records common conditions, equivalent groups,
engine-specific conditions, candidate inconsistencies, conflict scenarios,
and unresolved questions. These records are testing hypotheses rather than
confirmed IDE defects.

## 3. Generate and Materialize Tests

```powershell
python generate_tests_from_comparison.py `
  --comparison "outputs\comparison\InlineMethod_comparison_report.json" `
  --output-root "outputs\generated_tests\InlineMethod" `
  --tests-per-scenario 10 `
  --model "deepseek-v4-pro" `
  --base-url "$env:LLM_BASE_URL" `
  --thinking-mode disabled `
  --reasoning-effort= `
  --temperature 0.8 `
  --trace-level compact
```

The output contains independent Java programs and `test_cases.json`, which
stores each refactoring request and target.

Run the optional compilation and execution precheck before invoking an IDE:

```powershell
python refactoring_test_runner\run_compile_check.py `
  --test-root "outputs\generated_tests\InlineMethod"
```

## 4. Build the IDE Backends

The backends are compiled against local IDE installations. Their internal API
compatibility is IDE-version sensitive.

```powershell
python refactoring_test_runner\build_eclipse_backend.py `
  --eclipse-home "<ECLIPSE_HOME>" `
  --javac-path "<JDK_HOME>\bin\javac.exe" `
  --jar-path "<JDK_HOME>\bin\jar.exe"

python refactoring_test_runner\build_intellij_backend.py `
  --intellij-home "<INTELLIJ_HOME>" `
  --javac-path "<JDK_HOME>\bin\javac.exe" `
  --jar-path "<JDK_HOME>\bin\jar.exe"

python refactoring_test_runner\build_netbeans_backend.py `
  --netbeans-home "<NETBEANS_HOME>" `
  --javac-path "<JDK_HOME>\bin\javac.exe" `
  --jar-path "<JDK_HOME>\bin\jar.exe"
```

By default, generated backend artifacts and metadata are written below
`.cache/` and are not source files.

## 5. Run Differential Refactoring Tests

Execute each IDE against the same generated test root and use separate work
directories:

```powershell
python refactoring_test_runner\run_eclipse_refactoring_batch.py `
  --test-root "outputs\generated_tests\InlineMethod" `
  --eclipse-home "<ECLIPSE_HOME>" `
  --workspace-root ".cache\test_runner\eclipse_work" `
  --output "outputs\runner\eclipse_inline_method.json" `
  --java-path "<JDK_HOME>\bin\java.exe" `
  --javac-path "<JDK_HOME>\bin\javac.exe"

python refactoring_test_runner\run_intellij_refactoring_batch.py `
  --test-root "outputs\generated_tests\InlineMethod" `
  --intellij-home "<INTELLIJ_HOME>" `
  --workspace-root ".cache\test_runner\intellij_work" `
  --output "outputs\runner\intellij_inline_method.json" `
  --java-path "<JDK_HOME>\bin\java.exe" `
  --javac-path "<JDK_HOME>\bin\javac.exe"

python refactoring_test_runner\run_netbeans_refactoring_batch.py `
  --test-root "outputs\generated_tests\InlineMethod" `
  --netbeans-home "<NETBEANS_HOME>" `
  --workspace-root ".cache\test_runner\netbeans_work" `
  --output "outputs\runner\netbeans_inline_method.json" `
  --jdk-home "<JDK_HOME>" `
  --java-path "<JDK_HOME>\bin\java.exe" `
  --javac-path "<JDK_HOME>\bin\javac.exe"
```

The runners preserve engine decisions, diagnostics, changed Java files,
post-refactoring compilation results, and observable execution behavior.
Differences in these records are suspicious cases that require validation;
they are not automatically classified as bugs.
