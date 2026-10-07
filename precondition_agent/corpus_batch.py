import re

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional


GENERATED_RESULT_FILENAMES = {
    "precondition_profile.json",
    "raw_analyses.json",
    "batch_errors.json",
    "react_precondition_profile.json",
    "react_raw_analyses.json",
    "react_batch_errors.json",
}


@dataclass(frozen=True)
class CorpusUnit:
    engine: str
    refactoring: str
    phase: str
    path: Path
    relative_path: str
    entry_name: str
    unit_id: str


@dataclass(frozen=True)
class GroupOutputPaths:
    profile_path: Path
    raw_path: Path
    error_path: Path


def normalize_corpus_root(path: Path) -> Path:
    """Accept either the project root or the inner corpus directory."""
    if (path / "corpus").is_dir():
        return path / "corpus"
    return path


def discover_corpus_units(
    corpus_root: Path,
    refactoring: str,
    engine: str = "",
    phases: Optional[Iterable[str]] = None,
    implementation: str = "",
) -> List[CorpusUnit]:
    root = normalize_corpus_root(Path(corpus_root))
    allowed_phases = set(phases or ["required", "optional", "forbidden"])
    units: List[CorpusUnit] = []

    if not root.exists():
        raise FileNotFoundError(f"Corpus root not found: {root}")

    engine_dirs = [root / engine] if engine else sorted([item for item in root.iterdir() if item.is_dir()])
    for engine_dir in engine_dirs:
        refactoring_dir = engine_dir / refactoring
        if implementation:
            refactoring_dir = refactoring_dir / implementation
        if not refactoring_dir.is_dir():
            continue
        direct_files = sorted([item for item in refactoring_dir.iterdir() if _is_corpus_input_file(item)])
        if direct_files and "required" in allowed_phases:
            for file_path in direct_files:
                units.append(_make_unit(root, engine_dir.name, refactoring, "required", file_path))

        for phase_dir in sorted([item for item in refactoring_dir.iterdir() if item.is_dir()]):
            phase = phase_dir.name
            if phase not in allowed_phases:
                continue
            for file_path in sorted([item for item in phase_dir.iterdir() if _is_corpus_input_file(item)]):
                units.append(_make_unit(root, engine_dir.name, refactoring, phase, file_path))
    return units


def _is_corpus_input_file(path: Path) -> bool:
    """Return true for collected precondition snippets, false for generated outputs."""
    if not path.is_file():
        return False
    name = path.name
    lower_name = name.lower()
    if lower_name in GENERATED_RESULT_FILENAMES:
        return False
    if lower_name.endswith(".json"):
        return False
    if lower_name.startswith("."):
        return False
    if lower_name.endswith((".log", ".tmp", ".bak")):
        return False
    return True


def output_paths_for_unit(unit: CorpusUnit, output_root: Optional[Path] = None) -> GroupOutputPaths:
    if output_root:
        group_dir = Path(output_root) / unit.engine / unit.refactoring
        group_dir.mkdir(parents=True, exist_ok=True)
    elif unit.path.parent.name == unit.phase:
        group_dir = unit.path.parent.parent
    else:
        group_dir = unit.path.parent
    return GroupOutputPaths(
        profile_path=group_dir / "precondition_profile.json",
        raw_path=group_dir / "raw_analyses.json",
        error_path=group_dir / "batch_errors.json",
    )


def unit_to_dict(unit: CorpusUnit) -> dict:
    payload = asdict(unit)
    payload["path"] = str(unit.path)
    return payload


def infer_entry_name_from_corpus_file(path: Path) -> str:
    name = path.name
    if "." in name:
        return name.rsplit(".", 1)[-1]
    return name


def group_units(units: Iterable[CorpusUnit]) -> dict:
    grouped = {}
    for unit in units:
        grouped.setdefault((unit.engine, unit.refactoring), []).append(unit)
    return grouped


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")


def _make_unit(root: Path, engine: str, refactoring: str, phase: str, file_path: Path) -> CorpusUnit:
    relative_path = file_path.relative_to(root).as_posix()
    entry_name = infer_entry_name_from_corpus_file(file_path)
    unit_id = ".".join(
        [
            _safe_id(engine),
            _safe_id(refactoring),
            _safe_id(phase),
            _safe_id(file_path.name),
        ]
    )
    return CorpusUnit(
        engine=engine,
        refactoring=refactoring,
        phase=phase,
        path=file_path,
        relative_path=relative_path,
        entry_name=entry_name,
        unit_id=unit_id,
    )
