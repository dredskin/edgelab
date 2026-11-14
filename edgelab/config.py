from pathlib import Path
import yaml


def load_config(path="config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["_root"] = str(Path(path).resolve().parent)
    return cfg
