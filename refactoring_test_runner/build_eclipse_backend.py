import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


PLUGIN_ID = "experiment.eclipse.refactoring.backend"
PLUGIN_VERSION = "1.0.0"
APPLICATION_ID = "experiment.eclipse.refactoring.backend.application"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the headless Eclipse refactoring backend plugin.")
    parser.add_argument("--eclipse-home", required=True, help="Eclipse installation directory containing eclipsec.exe.")
    parser.add_argument("--javac-path", default=os.getenv("JAVAC_PATH", "javac"))
    parser.add_argument("--jar-path", default=os.getenv("JAR_PATH", "jar"))
    parser.add_argument("--source-root", default="eclipse_refactoring_backend")
    parser.add_argument("--output-root", default=".cache/eclipse_backend")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    eclipse_home = Path(args.eclipse_home).resolve()
    source_root = Path(args.source_root).resolve()
    output_root = Path(args.output_root).resolve()
    plugin_jar = output_root / f"{PLUGIN_ID}_{PLUGIN_VERSION}.jar"
    classes_dir = output_root / "classes"
    config_dir = output_root / "configuration"

    validate_eclipse_home(eclipse_home)
    if classes_dir.exists():
        shutil.rmtree(classes_dir)
    classes_dir.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)

    java_files = sorted((source_root / "src").rglob("*.java"))
    if not java_files:
        raise FileNotFoundError(f"No Java sources found under {source_root / 'src'}")

    classpath = classpath_for(eclipse_home)
    javac_args = [
        "-encoding",
        "UTF-8",
        "-source",
        "21",
        "-target",
        "21",
        "-cp",
        classpath,
        "-d",
        str(classes_dir),
    ] + [str(path) for path in java_files]
    argfile = output_root / "javac.args"
    argfile.write_text("\n".join(quote_arg(item) for item in javac_args), encoding="utf-8")
    compile_cmd = [
        args.javac_path,
        f"@{argfile}",
    ]
    run(compile_cmd, cwd=Path.cwd())

    manifest = source_root / "META-INF" / "MANIFEST.MF"
    plugin_xml = source_root / "plugin.xml"
    jar_cmd = [
        args.jar_path,
        "cfm",
        str(plugin_jar),
        str(manifest),
        "-C",
        str(classes_dir),
        ".",
        "-C",
        str(source_root),
        "plugin.xml",
    ]
    run(jar_cmd, cwd=Path.cwd())

    prepare_configuration(eclipse_home, config_dir, plugin_jar)
    payload = {
        "plugin_id": PLUGIN_ID,
        "application_id": APPLICATION_ID,
        "plugin_jar": str(plugin_jar),
        "configuration": str(config_dir),
        "eclipse_home": str(eclipse_home),
        "eclipsec": str(eclipse_home / "eclipsec.exe"),
    }
    metadata = output_root / "backend_metadata.json"
    metadata.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"BUILT {plugin_jar}")
    print(f"CONFIG {config_dir}")
    print(f"METADATA {metadata}")


def validate_eclipse_home(eclipse_home: Path) -> None:
    if not (eclipse_home / "eclipsec.exe").exists():
        raise FileNotFoundError(f"eclipsec.exe not found under {eclipse_home}")
    if not (eclipse_home / "configuration" / "config.ini").exists():
        raise FileNotFoundError(f"configuration/config.ini not found under {eclipse_home}")
    if not (eclipse_home / "configuration" / "org.eclipse.equinox.simpleconfigurator" / "bundles.info").exists():
        raise FileNotFoundError(f"simpleconfigurator bundles.info not found under {eclipse_home}")


def classpath_for(eclipse_home: Path) -> str:
    jars: List[str] = []
    jars.extend(str(path) for path in sorted((eclipse_home / "plugins").glob("*.jar")) if path.stat().st_size > 0)
    p2_plugins = Path.home() / ".p2" / "pool" / "plugins"
    if p2_plugins.exists():
        jars.extend(str(path) for path in sorted(p2_plugins.glob("*.jar")) if path.stat().st_size > 0)
    return os.pathsep.join(jars)


def prepare_configuration(eclipse_home: Path, config_dir: Path, plugin_jar: Path) -> None:
    if config_dir.exists():
        shutil.rmtree(config_dir)
    simple_dir = config_dir / "org.eclipse.equinox.simpleconfigurator"
    simple_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(eclipse_home / "configuration" / "config.ini", config_dir / "config.ini")
    source_bundles = eclipse_home / "configuration" / "org.eclipse.equinox.simpleconfigurator" / "bundles.info"
    text = source_bundles.read_text(encoding="utf-8")
    plugin_uri = plugin_jar.resolve().as_uri()
    line = f"{PLUGIN_ID},{PLUGIN_VERSION},{plugin_uri},4,true"
    if line not in text:
        text = text.rstrip() + "\n" + line + "\n"
    (simple_dir / "bundles.info").write_text(text, encoding="utf-8")


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
