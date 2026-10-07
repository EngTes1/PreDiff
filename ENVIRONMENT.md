# Environment

This document describes the software and external resources required to run
the packaged PreDiff pipeline. Python packages are listed separately in
`requirements.txt`; this file records the wider execution environment that
cannot be installed with `pip` alone.

The paths shown in commands are placeholders. PreDiff does not require the
corpus, IDE source repositories, or IDE installations to be placed at fixed
locations.

## 1. Reference Environment

The packaged code was checked on the following host environment:

| Component | Reference version |
| --- | --- |
| Operating system | Windows 11, version 10.0.22631 (64-bit) |
| PowerShell | Windows PowerShell 5.1.22621.6133 |
| Python | 3.12.7 (Anaconda distribution) |
| `langchain-openai` | 1.1.8 |
| JDK used to verify the Java AST helper | Eclipse Temurin 17.0.19 |
| ripgrep | 15.1.0 |
| Git | 2.49.0.windows.1 |

Python 3.10 or later is expected. JDK 17 or later is sufficient for the
precondition-analysis pipeline. JDK 21 is recommended for the complete
IDE-runner workflow, subject to the requirements of the selected IDE release.

## 2. Python Environment

Create an isolated virtual environment from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The required Python dependency is pinned in `requirements.txt`:

```text
langchain-openai==1.1.8
```

The Python runner and orchestration code otherwise use the standard library.

### Optional Kotlin support

The main Java experiment does not require a Kotlin parser. Install the
optional parser only when Kotlin snippets must be analyzed:

```powershell
python -m pip install tree-sitter-language-pack
```

Because this dependency is optional and was not required for the Java-only
main experiment, it is not pinned in `requirements.txt`.

## 3. JDK Configuration

A full JDK is required; a JRE alone is insufficient. The JDK provides:

- `java`, used to run Java programs and the Java AST helper;
- `javac`, used to compile the AST helper and generated test programs; and
- `jar`, used when building the IDE backend artifacts.

The precondition-analysis code automatically compiles
`tools/java_ast_helper/JavaAstParserCli.java` into `.ast_helper_build/` on its
first AST-enabled run. The helper was successfully compiled with Temurin JDK
17.0.19.

For reproducible runner execution, pass one JDK installation explicitly
instead of relying on whichever executable appears first on `PATH`:

```powershell
$env:JDK_HOME = "<JDK_HOME>"
$env:JAVA_PATH = "$env:JDK_HOME\bin\java.exe"
$env:JAVAC_PATH = "$env:JDK_HOME\bin\javac.exe"
```

Backend build commands also require:

```text
<JDK_HOME>\bin\jar.exe
```

Generated programs may use language features newer than Java 17. In that
case, select a JDK that supports those features and use the same compiler
configuration for all compared engines.

## 4. Source Retrieval Tools

PreDiff uses two local source-retrieval mechanisms:

- the packaged JDK-based AST helper for structured Java declarations and code
  blocks; and
- `ripgrep` (`rg`) as the textual-search fallback.

Install ripgrep and confirm that it is visible on `PATH`:

```powershell
rg --version
```

Alternatively, provide its executable through `--rg-path` when running
`batch_analyze_corpus.py`.

## 5. LLM Endpoint

PreDiff calls an OpenAI-compatible chat-completions endpoint through
`langchain-openai`. Set credentials in the current shell rather than storing
them in source files:

```powershell
$env:DEEPSEEK_API_KEY = "<YOUR_API_KEY>"
$env:LLM_BASE_URL = "<OPENAI_COMPATIBLE_ENDPOINT>"
```

`OPENAI_API_KEY` is accepted as a fallback when `DEEPSEEK_API_KEY` is not set.
The scripts do not interactively request an API key and do not automatically
load a `.env` file. A missing key causes a normal non-dry run to stop before
the LLM request.

The experiment commands in `README.md` explicitly set the stage-specific
models and parameters:

| Stage | Model identifier | Temperature | Thinking mode |
| --- | --- | ---: | --- |
| Precondition analysis | `deepseek-flash` | 0.0 | disabled |
| Cross-engine comparison | `deepseek-v4-pro` | 0.0 | disabled |
| Test generation | `deepseek-v4-pro` | 0.8 | disabled |

Model identifiers are provider-specific. If another endpoint uses different
names, pass its identifiers with `--model`. Command-line options should be
used for reproduction because some scripts retain local development defaults
for backward compatibility.

Useful optional environment variables are:

| Variable | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY` | Preferred API credential |
| `OPENAI_API_KEY` | Fallback API credential |
| `LLM_BASE_URL` | OpenAI-compatible endpoint URL |
| `MODEL_NAME` | Fallback model name when `--model` is omitted |
| `JAVA_PATH` | Java launcher used by precondition analysis |
| `JAVAC_PATH` | Java compiler used by precondition analysis |

Secrets must not be committed to Git or written into experiment reports.

## 6. External Source Repositories

Precondition recovery requires complete local source trees for the analyzed
engines. Supply their locations explicitly with repeated `--repo-root`
arguments:

```powershell
--repo-root "Eclipse=<ECLIPSE_JDT_UI_SOURCE>" `
--repo-root "IntelliJ IDEA=<INTELLIJ_COMMUNITY_SOURCE>" `
--repo-root "netbeans=<NETBEANS_SOURCE>"
```

The expected repositories are:

| Engine | Source repository |
| --- | --- |
| Eclipse | Eclipse JDT UI source tree |
| IntelliJ IDEA | IntelliJ Community source tree |
| NetBeans | Apache NetBeans source tree |

Repository revisions affect the recovered preconditions. Record the exact
commit, tag, archive identity, and any local modifications in
`SOURCE_REVISIONS.md` before publishing an experimental artifact. Directory
names such as `master` are not sufficient revision identifiers.

## 7. Input Corpus

The precondition corpus is an external input and is not included in this
source-only package. Its root is supplied with `--corpus-root`. The main
experiment analyzes method-level snippets under each operation's `required`
directory. For layouts that store snippet files directly in the selected
operation directory, the discovery code assigns those files to the `required`
phase as well.

At minimum, a corpus unit identifies:

- the refactoring engine;
- the refactoring operation;
- the collection phase, normally `required`; and
- the collected method-level source snippet.

When a refactoring has multiple implementation variants, select the intended
variant explicitly with `--implementation`. For example, the legacy IntelliJ
IDEA Extract Method implementation is selected with `--implementation legacy`
together with the IntelliJ IDEA engine filter.

The three Rename operations use nested corpus paths and must be passed as
`Rename/RenameMethod`, `Rename/RenameField`, and `Rename/RenameVariable` to
`batch_analyze_corpus.py`. Their generated profiles can still be selected in
the comparison stage by the short names `RenameMethod`, `RenameField`, and
`RenameVariable`, or passed explicitly with repeated `--profile` arguments.

## 8. IDE Installations and Backend Compatibility

Differential execution requires local installations of the tested IDEs. The
following installations were detected on the reference machine when this
document was prepared:

| Engine | Detected reference installation |
| --- | --- |
| Eclipse | Eclipse IDE 2026-06, Platform 4.40.0 |
| IntelliJ IDEA | 2026.1, build 261.22158.277 |
| Apache NetBeans | 28 |

These values describe the available reference installations; they do not
replace the version information that must accompany each reported experiment.

The backend code calls IDE-internal refactoring APIs, so binary compatibility
is version-sensitive. Rebuild every backend against the exact IDE installation
used for execution:

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

A backend compile failure usually indicates an IDE API-version mismatch, a
wrong installation path, or an incompatible JDK. It must not be interpreted
as a refactoring-engine decision.

The packaged and documented main runner workflow covers Eclipse, IntelliJ
IDEA, and NetBeans. Experimental VS Code runner files are present in the code
base but are not included in the verified environment matrix above.

## 9. Generated Directories

The following directories are generated at runtime and are not part of the
source environment:

| Directory | Contents |
| --- | --- |
| `.ast_helper_build/` | Compiled Java AST helper classes |
| `.cache/` | Source indexes, backend builds, metadata, and runner workspaces |
| `outputs/` | Analysis, comparison, generation, and runner results when using the README commands |
| `.venv/` | Optional local Python virtual environment |

They can be regenerated from source and are excluded by `.gitignore`.

## 10. Environment Verification

Run these commands before a full experiment:

```powershell
python --version
python -m pip show langchain-openai
java -version
javac -version
rg --version
git --version
```

Then verify corpus discovery without making an LLM request:

```powershell
python batch_analyze_corpus.py `
  --corpus-root "<CORPUS_ROOT>" `
  --refactoring "InlineMethod" `
  --phase required `
  --repo-root "Eclipse=<ECLIPSE_JDT_UI_SOURCE>" `
  --repo-root "IntelliJ IDEA=<INTELLIJ_COMMUNITY_SOURCE>" `
  --repo-root "netbeans=<NETBEANS_SOURCE>" `
  --dry-run
```

Before publishing results, record the corpus revision, IDE source revisions,
IDE binary versions, JDK version, model endpoint and identifiers, run date,
and command-line arguments. These values are necessary to distinguish an
environment difference from an analysis or refactoring-engine difference.
