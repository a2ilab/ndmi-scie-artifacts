"""Repository-relative defaults; optional overrides are environment variables."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]

def path_setting(name, default):
    value = os.environ.get(name) or default
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else ROOT / path)

def output_path(relative):
    root = Path(path_setting("SCIE_OUTPUT_DIR", "outputs")).resolve()
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError("Output path must stay inside SCIE_OUTPUT_DIR")
    protected = (ROOT / "data", ROOT / "prompts", ROOT / "provenance", ROOT / "src", ROOT / ".local")
    if any(result.is_relative_to(path.resolve()) for path in protected):
        raise ValueError("Outputs must not overwrite repository inputs or source files")
    result.parent.mkdir(parents=True, exist_ok=True)
    return str(result)
