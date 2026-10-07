import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


MODULE_ID = "experiment.netbeans.refactoring.backend"
MODULE_JAR_NAME = "experiment-netbeans-refactoring-backend.jar"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the headless NetBeans refactoring backend module.")
    parser.add_argument("--netbeans-home", required=True, help="NetBeans installation directory.")
    parser.add_argument("--javac-path", default=os.getenv("JAVAC_PATH", "javac"))
    parser.add_argument("--jar-path", default=os.getenv("JAR_PATH", "jar"))
    parser.add_argument("--source-root", default="netbeans_refactoring_backend")
    parser.add_argument("--output-root", default=".cache/netbeans_backend")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    netbeans_home = normalize_netbeans_home(Path(args.netbeans_home).resolve())
    source_root = Path(args.source_root).resolve()
    output_root = Path(args.output_root).resolve()
    classes_dir = output_root / "classes"
    cluster = output_root / "cluster"
    module_jar = cluster / "modules" / MODULE_JAR_NAME
    validate_netbeans_home(netbeans_home)

    if classes_dir.exists():
        shutil.rmtree(classes_dir)
    if cluster.exists():
        shutil.rmtree(cluster)
    classes_dir.mkdir(parents=True, exist_ok=True)
    module_jar.parent.mkdir(parents=True, exist_ok=True)

    java_files = sorted((source_root / "src").rglob("*.java"))
    classpath = classpath_for(netbeans_home)
    javac_args = [
        "-encoding",
        "UTF-8",
        "-source",
        "21",
        "-target",
        "21",
        "-proc:none",
        "-cp",
        classpath,
        "-d",
        str(classes_dir),
    ] + [str(path) for path in java_files]
    argfile = output_root / "javac.args"
    output_root.mkdir(parents=True, exist_ok=True)
    argfile.write_text("\n".join(quote_arg(item) for item in javac_args), encoding="utf-8")
    run([args.javac_path, f"@{argfile}"], cwd=Path.cwd())

    manifest = source_root / "META-INF" / "MANIFEST.MF"
    run([
        args.jar_path,
        "cfm",
        str(module_jar),
        str(manifest),
        "-C",
        str(classes_dir),
        ".",
    ], cwd=Path.cwd())

    write_module_config(cluster)
    metadata = {
        "module_id": MODULE_ID,
        "module_jar": str(module_jar),
        "cluster": str(cluster),
        "netbeans_home": str(netbeans_home),
        "launcher": str(netbeans_home / "bin" / "netbeans64.exe"),
    }
    metadata_path = output_root / "backend_metadata.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"BUILT {module_jar}")
    print(f"CLUSTER {cluster}")
    print(f"METADATA {metadata_path}")


def validate_netbeans_home(netbeans_home: Path) -> None:
    if not (netbeans_home / "bin" / "netbeans64.exe").exists():
        raise FileNotFoundError(f"netbeans64.exe not found under {netbeans_home}")
    if not (netbeans_home / "etc" / "netbeans.clusters").exists():
        raise FileNotFoundError(f"etc/netbeans.clusters not found under {netbeans_home}")


def normalize_netbeans_home(path: Path) -> Path:
    if path.name.lower() == "bin" and (path.parent / "etc" / "netbeans.clusters").exists():
        return path.parent
    return path


def classpath_for(netbeans_home: Path) -> str:
    jars: List[str] = []
    for cluster in netbeans_home.iterdir():
        if not cluster.is_dir():
            continue
        jars.extend(str(path) for path in sorted((cluster / "modules").glob("*.jar")) if path.stat().st_size > 0)
        jars.extend(str(path) for path in sorted((cluster / "lib").glob("*.jar")) if path.stat().st_size > 0)
        jars.extend(str(path) for path in sorted((cluster / "core").glob("*.jar")) if path.stat().st_size > 0)
    return os.pathsep.join(jars)


def write_module_config(cluster: Path) -> None:
    config = cluster / "config" / "Modules"
    config.mkdir(parents=True, exist_ok=True)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE module PUBLIC "-//NetBeans//DTD Module Status 1.0//EN"
                        "http://www.netbeans.org/dtds/module-status-1_0.dtd">
<module name="{MODULE_ID}">
    <param name="enabled">true</param>
    <param name="jar">modules/{MODULE_JAR_NAME}</param>
    <param name="reloadable">false</param>
</module>
"""
    (config / "experiment-netbeans-refactoring-backend.xml").write_text(xml, encoding="utf-8")


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
