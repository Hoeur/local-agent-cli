"""Saved settings: one or more model profiles, and which one is active.

config.json looks like this (older files with just base_url and model still load):

    {"active": "deepseek",
     "profiles": {"deepseek": {"provider": "deepseek", "base_url": "...", "model": "deepseek-chat",
                               "wire": "chat", "context_chars": 100000}}}

API keys are never stored here; see providers.py.
"""
import json
import os
import tempfile
from pathlib import Path

# Roughly 3,000-4,000 tokens, which fits Ollama's small default context window.
DEFAULT_CONTEXT_CHARS = 12_000
MIN_CONTEXT_CHARS = 4_000
MAX_CONTEXT_CHARS = 2_000_000
PROFILE_FIELDS = ("provider", "base_url", "model", "wire", "context_chars", "max_tokens")


def parse_context_chars(value):
    if isinstance(value, bool):
        value = None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("The context budget must be a whole number of characters.") from None
    if not MIN_CONTEXT_CHARS <= number <= MAX_CONTEXT_CHARS:
        raise ValueError(
            f"Choose a context budget between {MIN_CONTEXT_CHARS:,} and {MAX_CONTEXT_CHARS:,} characters."
        )
    return number


def parse_max_tokens(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= 1_000_000:
        raise ValueError("max_tokens must be a whole number between 16 and 1,000,000.")
    return value


def read_settings(directory: Path):
    """Return the saved settings in the current format, upgrading an older single-model file."""
    from .providers import guess_provider

    path = directory / "config.json"
    if not path.exists():
        return {"active": None, "profiles": {}}
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("The saved config must be a JSON object.")
    if "profiles" not in settings:
        profile = {key: settings[key] for key in ("base_url", "model", "context_chars") if key in settings}
        if not profile:
            return {"active": None, "profiles": {}}
        name = guess_provider(profile.get("base_url"))
        profile.setdefault("context_chars", DEFAULT_CONTEXT_CHARS)
        return {"active": name, "profiles": {name: {"provider": name, **profile}}}
    if not isinstance(settings["profiles"], dict) or not all(
        isinstance(profile, dict) for profile in settings["profiles"].values()
    ):
        raise ValueError("The saved config has an invalid profiles section.")
    return settings


def write_settings(directory: Path, settings):
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(settings, stream, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, directory / "config.json")
    finally:
        temporary.unlink(missing_ok=True)


def load_config(directory: Path, profile: str | None = None):
    """Return the active (or named) profile as a flat dict, with environment overrides applied."""
    from .providers import guess_provider

    settings = read_settings(directory)
    name = profile or os.environ.get("AGENT_PROFILE") or settings.get("active")
    profiles = settings["profiles"]
    if profile or os.environ.get("AGENT_PROFILE"):
        if name not in profiles:
            raise ValueError(f"No saved profile {name!r}. Saved: {', '.join(profiles) or 'none'}.")
    config = dict(profiles.get(name) or {})
    for key in ("base_url", "model"):
        config[key] = os.environ.get(f"AGENT_{key.upper()}", config.get(key, ""))
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(
                f"No model is set up for this folder (looked in {directory.resolve() / 'config.json'}). Run, "
                "for example: python -m local_agent setup --model qwen3:4b  (local Ollama) or "
                "python -m local_agent setup --provider deepseek  (cloud). Settings are saved per folder; "
                "--data-dir picks another one."
            )
        config[key] = config[key].strip()
    if os.environ.get("AGENT_BASE_URL"):
        config["provider"] = guess_provider(config["base_url"])
        config.pop("wire", None)
    config["provider"] = config.get("provider") or guess_provider(config["base_url"])
    config["profile"] = name or config["provider"]
    config["context_chars"] = parse_context_chars(
        os.environ.get("AGENT_CONTEXT_CHARS", config.get("context_chars", DEFAULT_CONTEXT_CHARS))
    )
    config["max_tokens"] = parse_max_tokens(config.get("max_tokens"))
    return config


def save_config(directory: Path, base_url: str, model: str, context_chars: int = DEFAULT_CONTEXT_CHARS, *,
                provider: str | None = None, wire: str | None = None, name: str | None = None,
                max_tokens: int | None = None):
    """Save a profile and make it the active one."""
    from .providers import check_profile_name, guess_provider

    settings = read_settings(directory)
    provider = provider or guess_provider(base_url)
    name = check_profile_name(name or provider)
    profile = {"provider": provider, "base_url": base_url, "model": model,
               "context_chars": parse_context_chars(context_chars)}
    if wire:
        profile["wire"] = wire
    if parse_max_tokens(max_tokens):
        profile["max_tokens"] = max_tokens
    settings["profiles"][name] = profile
    settings["active"] = name
    write_settings(directory, settings)
    return name


def set_active(directory: Path, name: str, model: str | None = None):
    """Make a saved profile active, optionally changing its model."""
    settings = read_settings(directory)
    if name not in settings["profiles"]:
        raise ValueError(f"No saved profile {name!r}.")
    if model:
        settings["profiles"][name]["model"] = model
    settings["active"] = name
    write_settings(directory, settings)
