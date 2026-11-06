"""Known model services, API-key lookup and the private key store.

A *profile* (saved in .agent/config.json) is a provider plus a model, base URL and budget. Several
profiles can be saved and switched with `use <name>` or `/provider <name>` in the terminal UI.

API keys are never written to the project folder. They come from, in order:
1. the provider's environment variable (DEEPSEEK_API_KEY, ANTHROPIC_API_KEY, ...),
2. the key store in your user settings folder, filled by `key set <profile>`,
3. AGENT_API_KEY, for local servers and custom endpoints only.
"""
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .provider import ChatProvider
from .provider_anthropic import AnthropicProvider
from .provider_responses import ResponsesProvider

WIRES = {"chat": ChatProvider, "anthropic": AnthropicProvider, "responses": ResponsesProvider}
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,39}")


@dataclass(frozen=True)
class Preset:
    label: str
    wire: str
    base_url: str | None
    key_env: str | None  # None: no key needed (local servers).
    model: str | None    # Default model, if there is a sensible one.
    context_chars: int
    note: str


PRESETS = {
    "ollama": Preset("Ollama (local)", "chat", "http://127.0.0.1:11434/v1", None, None, 12_000,
                     "free, runs on your computer"),
    "anthropic": Preset("Claude (Anthropic)", "anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY",
                        "claude-sonnet-5-5", 150_000, "keys at console.anthropic.com"),
    "openai": Preset("OpenAI / Codex", "responses", "https://api.openai.com/v1", "OPENAI_API_KEY",
                     "gpt-5.5", 150_000, "keys at platform.openai.com; Codex models need this Responses API"),
    "deepseek": Preset("DeepSeek", "chat", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY",
                       "deepseek-chat", 100_000, "keys at platform.deepseek.com"),
    "alibaba": Preset("Alibaba Model Studio (Singapore)", "chat",
                      "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
                      "qwen3-coder-plus", 150_000, "pay as you go: Qwen, DeepSeek, Kimi, GLM"),
    "alibaba-us": Preset("Alibaba Model Studio (Virginia)", "chat",
                         "https://dashscope-us.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
                         "qwen3-coder-plus", 150_000, "US region key"),
    "alibaba-cn": Preset("Alibaba Model Studio (Beijing)", "chat",
                         "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
                         "qwen3-coder-plus", 150_000, "China region key"),
    "alibaba-coding": Preset("Alibaba Coding Plan (subscription)", "chat",
                             "https://coding-intl.dashscope.aliyuncs.com/v1", "DASHSCOPE_API_KEY",
                             "qwen3-coder-plus", 150_000, "monthly plan for coding tools; its own key"),
    "openrouter": Preset("OpenRouter", "chat", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                         None, 150_000, "one key for Claude, GPT, DeepSeek, Qwen and more"),
    "custom": Preset("Custom / reseller", "chat", None, "AGENT_API_KEY", None, 100_000,
                     "any OpenAI- or Anthropic-compatible endpoint (pass --base-url)"),
}
ALIASES = {"claude": "anthropic", "codex": "openai", "gpt": "openai", "chatgpt": "openai", "qwen": "alibaba",
           "dashscope": "alibaba", "bailian": "alibaba", "modelstudio": "alibaba", "local": "ollama",
           "reseller": "custom"}


def preset_name(name):
    name = (name or "").strip().lower()
    return ALIASES.get(name, name) if ALIASES.get(name, name) in PRESETS else None


def guess_provider(base_url):
    """Which preset a bare base URL (from an older config or AGENT_BASE_URL) belongs to."""
    url = urlsplit(base_url or "")
    for name, preset in PRESETS.items():
        if preset.base_url and urlsplit(preset.base_url).netloc == url.netloc and name != "ollama":
            return name
    if url.hostname in {"localhost", "127.0.0.1", "::1"}:
        return "ollama" if url.port == 11434 else "custom"
    return "custom"


def check_profile_name(name):
    if not PROFILE_NAME.fullmatch(name or ""):
        raise ValueError("Profile names use lowercase letters, numbers, dots, hyphens or underscores (max 40).")
    return name


# ── Key store ────────────────────────────────────────────────────────────────────────────


def key_path():
    if os.environ.get("AGENT_KEYS_FILE"):
        return Path(os.environ["AGENT_KEYS_FILE"])
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "local-agent" / "keys.json"
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "local-agent" / "keys.json"


def load_keys():
    path = key_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, dict):
        raise ValueError(f"The key store {path} is damaged. Delete it and add your keys again.")
    return {name: value for name, value in keys.items() if isinstance(value, str)}


def save_keys(keys):
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)  # Owner only, on systems that support it.
        json.dump({"keys": keys}, stream, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def check_key(value):
    # Drop what terminals add around a paste: control characters (Ctrl+V in a hidden prompt sends one),
    # a byte-order mark, quotes, and surrounding spaces. The key itself is never shown in errors.
    value = "".join(char for char in (value or "") if char.isprintable() and char != "\ufeff").strip().strip('"\'')
    if not value:
        raise ValueError("Nothing was pasted. In Command Prompt, paste with a right-click (Ctrl+V doesn't work in "
                         "a hidden prompt), or run `key set <name> --show` to see what you type.")
    problems = []
    if not 8 <= len(value) <= 500:
        problems.append(f"it is {len(value)} characters long")
    if any(char.isspace() for char in value):
        problems.append("it contains spaces")
    if any(not 33 <= ord(char) < 127 for char in value if not char.isspace()):
        problems.append("it contains non-ASCII characters")
    if problems:
        raise ValueError("That doesn't look like an API key: " + " and ".join(problems) + ". Copy it again "
                         "from the provider's website, or run `key set <name> --show` to see what you paste.")
    return value


def set_key(name, value):
    keys = load_keys()
    keys[check_profile_name(name)] = check_key(value)
    save_keys(keys)


def remove_key(name):
    keys = load_keys()
    if keys.pop(name, None) is None:
        return False
    save_keys(keys)
    return True


def resolve_key(profile):
    """Return (key, where it came from) for a profile, or (None, None)."""
    preset = PRESETS.get(profile.get("provider")) or PRESETS["custom"]
    env = preset.key_env
    if env and env != "AGENT_API_KEY" and os.environ.get(env, "").strip():
        return os.environ[env].strip(), env
    stored = load_keys()
    for name in (profile.get("profile"), profile.get("provider")):
        if name and stored.get(name):
            return stored[name], "key store"
    if profile.get("provider") in {"ollama", "custom"} and os.environ.get("AGENT_API_KEY", "").strip():
        return os.environ["AGENT_API_KEY"].strip(), "AGENT_API_KEY"
    return None, None


def mask(key):
    return f"…{key[-4:]}" if key and len(key) > 8 else "set"


def make_image_provider(config):
    from .provider import ImageProvider

    model = config.get("image_model")
    if not model:
        return None
    chat_url = config["base_url"].rstrip("/")
    image_url = (config.get("image_base_url") or chat_url).rstrip("/")
    key = os.environ.get("AGENT_IMAGE_API_KEY", "").strip()
    if not key and image_url == chat_url:
        key, _ = resolve_key(config)
    return ImageProvider(image_url, model, key)


def make_provider(config, require_key=True):
    """Build the provider object for a loaded profile."""
    provider = config.get("provider") or guess_provider(config.get("base_url"))
    preset = PRESETS.get(provider) or PRESETS["custom"]
    wire = config.get("wire") or preset.wire
    if wire not in WIRES:
        raise ValueError(f"Unknown API format {wire!r}. Use one of: {', '.join(WIRES)}.")
    key, _ = resolve_key({**config, "provider": provider})
    if require_key and not key and preset.key_env and provider != "custom":
        name = config.get("profile") or provider
        raise ValueError(f"No API key for {name}. Run `python -m local_agent key set {name}` "
                         f"or set {preset.key_env}.")
    return WIRES[wire](config["base_url"], config["model"], key, max_tokens=config.get("max_tokens"))
