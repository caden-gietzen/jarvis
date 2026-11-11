from pathlib import Path

import os

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / ".jarvis_state.json"
LOG_DIR = ROOT / "logs"


# The notes folder (HANDOFF.md, open-questions.md, ...). Override with the JARVIS_WORKSPACE_ROOT
# environment variable; the default is a "jarvis-workspace" folder next to this repo.
WORKSPACE_ROOT = Path(os.environ.get("JARVIS_WORKSPACE_ROOT", str(ROOT.parent / "jarvis-workspace")))


def workspace_missing_message(root: Path = None) -> "str | None":
    """One line for the log or for speech when the workspace folder is absent, else None."""
    root = Path(root or WORKSPACE_ROOT)
    if root.is_dir():
        return None
    return (f"workspace folder not found: {root} (see SETUP.md section 3a, or run "
            "scripts/ensure_workspace.py --clone)")


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Relative folders in config.yaml are relative to this repo, so ".." is the folder that holds the repo.
    if isinstance(cfg, dict):
        if cfg.get("project_dir"):
            cfg["project_dir"] = str((ROOT / str(cfg["project_dir"])).resolve())
        if cfg.get("allowed_dirs"):
            cfg["allowed_dirs"] = [str((ROOT / str(d)).resolve()) for d in cfg["allowed_dirs"]]
    return cfg
