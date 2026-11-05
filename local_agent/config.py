import json
import os
import tempfile
from pathlib import Path


def load_config(directory: Path):
    path = directory / "config.json"
    config = {}
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("The saved config must be a JSON object.")
    for key in ("base_url", "model"):
        config[key] = os.environ.get(f"AGENT_{key.upper()}", config.get(key, ""))
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError("Run setup or set AGENT_BASE_URL and AGENT_MODEL before chatting.")
        config[key] = config[key].strip()
    for key in ("image_model", "image_base_url"):
        value = os.environ.get(f"AGENT_{key.upper()}", config.get(key, ""))
        if not isinstance(value, str):
            raise ValueError(f"The {key} setting must be text.")
        config[key] = value.strip()
    return config


def save_config(directory: Path, base_url: str, model: str, *, image_model="", image_base_url=""):
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump({"base_url": base_url, "model": model,
                   "image_model": image_model, "image_base_url": image_base_url}, stream, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, directory / "config.json")
    finally:
        temporary.unlink(missing_ok=True)
