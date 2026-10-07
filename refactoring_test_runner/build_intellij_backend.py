import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


PLUGIN_ID = "experiment.intellij.refactoring.backend"
PLUGIN_VERSION = "1.0.0"
PLUGIN_JAR_NAME = "experiment-intellij-refactoring-backend.jar"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the IntelliJ IDEA headless refactoring backend plugin.")
    parser.add_argument("--intellij-home", required=True, help="IntelliJ IDEA installation directory or its bin directory.")
    parser.add_argument("--javac-path", default=os.getenv("JAVAC_PATH", "javac"))
    parser.add_argument("--jar-path", default=os.getenv("JAR_PATH", "jar"))
    parser.add_argument("--source-root", default="intellij_refactoring_backend")
    parser.add_argument("--output-root", default=".cache/intellij_backend")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    intellij_home = normalize_intellij_home(Path(args.intellij_home).resolve())
    source_root = Path(args.source_root).resolve()
    output_root = Path(args.output_root).resolve()
    classes_dir = output_root / "classes"
    plugin_dir = output_root / "plugins" / "refactoring-test-backend"
    plugin_jar = plugin_dir / "lib" / PLUGIN_JAR_NAME

    validate_intellij_home(intellij_home)
    if classes_dir.exists():
        shutil.rmtree(classes_dir)
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    classes_dir.mkdir(parents=True, exist_ok=True)
    plugin_jar.parent.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)

    java_files = sorted((source_root / "src").rglob("*.java"))
    if not java_files:
        raise FileNotFoundError(f"No Java sources found under {source_root / 'src'}")

    javac_args = [
        "-encoding",
        "UTF-8",
        "-source",
        "21",
        "-target",
        "21",
        "-proc:none",
        "-cp",
        classpath_for(intellij_home),
        "-d",
        str(classes_dir),
    ] + [str(path) for path in java_files]
    argfile = output_root / "javac.args"
    argfile.write_text("\n".join(quote_arg(item) for item in javac_args), encoding="utf-8")
    run([args.javac_path, f"@{argfile}"], cwd=Path.cwd())

    run(
        [
            args.jar_path,
            "cf",
            str(plugin_jar),
            "-C",
            str(classes_dir),
            ".",
            "-C",
            str(source_root),
            "META-INF/plugin.xml",
        ],
        cwd=Path.cwd(),
    )

    metadata = {
        "plugin_id": PLUGIN_ID,
        "plugin_version": PLUGIN_VERSION,
        "plugin_dir": str(plugin_dir),
        "plugin_root": str(plugin_dir.parent),
        "plugin_jar": str(plugin_jar),
        "intellij_home": str(intellij_home),
        "launcher": str(intellij_home / "bin" / "idea.bat"),
    }
    metadata_path = output_root / "backend_metadata.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"BUILT {plugin_jar}")
    print(f"PLUGIN_ROOT {plugin_dir.parent}")
    print(f"METADATA {metadata_path}")


def normalize_intellij_home(path: Path) -> Path:
    if path.name.lower() == "bin" and (path.parent / "product-info.json").exists():
        return path.parent
    return path


def validate_intellij_home(intellij_home: Path) -> None:
    if not (intellij_home / "product-info.json").exists():
        raise FileNotFoundError(f"product-info.json not found under {intellij_home}")
    if not (intellij_home / "bin" / "idea.bat").exists():
        raise FileNotFoundError(f"idea.bat not found under {intellij_home / 'bin'}")
    if not (intellij_home / "plugins" / "java" / "lib" / "java-impl.jar").exists():
        raise FileNotFoundError(f"Java plugin jar not found under {intellij_home}")


def classpath_for(intellij_home: Path) -> str:
    jars: List[str] = []
    jars.extend(str(path) for path in sorted((intellij_home / "lib").glob("*.jar")) if path.stat().st_size > 0)
    java_lib = intellij_home / "plugins" / "java" / "lib"
    if java_lib.exists():
        jars.extend(str(path) for path in sorted(java_lib.rglob("*.jar")) if path.stat().st_size > 0)
    return os.pathsep.join(jars)


def run(command: List[str], cwd: Path) -> None:
    process = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if process.returncode != 0:
        sys.stdout.write(process.stdout)
        sys.stderr.write(process.stderr)
        raise RuntimeError(f"command failed with exit code {process.returncode}: {' '.join(command)}")


def quote_arg(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


if __name__ == "__main__":
    main()
