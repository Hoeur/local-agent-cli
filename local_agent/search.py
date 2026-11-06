"""Which web search provider web_search uses, and the optional Brave Search API.

The default is DuckDuckGo's HTML page, no key needed; AGENT_SEARCH_URL still points it at your own
DuckDuckGo-compatible page. Brave Search (https://api-dashboard.search.brave.com/app/documentation/
web-search/get-started) is a general search API you can opt into:

    local-agent web-search brave
    local-agent key set brave-search      (or set BRAVE_SEARCH_API_KEY)

The choice is saved in the project's config.json; the key never is. It comes from the
BRAVE_SEARCH_API_KEY variable or the private key store used for model keys. AGENT_SEARCH_PROVIDER
overrides the saved choice for one run. Web tools still only run with --web or /web on.
"""
import html
import json
import os
import re
from urllib.parse import urlencode, urlsplit

PROVIDERS = {
    "duckduckgo": "DuckDuckGo (default, no key)",
    "brave": "Brave Search API (needs a key)",
}
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_KEY_ENV = "BRAVE_SEARCH_API_KEY"
BRAVE_KEY_NAME = "brave-search"


def check_provider(name):
    name = (name or "").strip().lower()
    if name not in PROVIDERS:
        raise ValueError(f"Unknown search provider {name!r}. Choose from: {', '.join(PROVIDERS)}.")
    return name


def configured(data_dir):
    """The search provider for this data directory: AGENT_SEARCH_PROVIDER, then config.json."""
    from .config import read_settings

    if os.environ.get("AGENT_SEARCH_PROVIDER", "").strip():
        return check_provider(os.environ["AGENT_SEARCH_PROVIDER"])
    try:
        section = read_settings(data_dir).get("web_search") or {}
    except (OSError, ValueError):
        return "duckduckgo"
    return check_provider(section.get("provider") or "duckduckgo") if isinstance(section, dict) else "duckduckgo"


def save(data_dir, name):
    from .config import read_settings, write_settings

    settings = read_settings(data_dir)
    settings["web_search"] = {"provider": check_provider(name)}
    write_settings(data_dir, settings)


def brave_key():
    """Return (key, where it came from), or (None, None). Keys are never read from the project."""
    from .providers import load_keys

    if os.environ.get(BRAVE_KEY_ENV, "").strip():
        return os.environ[BRAVE_KEY_ENV].strip(), BRAVE_KEY_ENV
    stored = load_keys().get(BRAVE_KEY_NAME)
    return (stored, "key store") if stored else (None, None)


def label(provider, search_url=None):
    """How results name their source."""
    if provider == "brave":
        return "Brave Search"
    custom = search_url or os.environ.get("AGENT_SEARCH_URL")
    if custom:
        return f"custom search ({urlsplit(custom).netloc or custom})"
    return "DuckDuckGo"


def plain(value, limit=300):
    """Brave marks matches with <strong>; keep the words, not the markup."""
    text = html.unescape(re.sub(r"<[^>]+>", "", str(value or "")))
    return " ".join(text.split())[:limit]


def brave_search(query, limit=6, allow_private=False):
    from .tools import HTTPStatusError, PrivateWebAddress, fetch

    key, _ = brave_key()
    if not key:
        raise ValueError(f"Brave Search needs an API key. Set {BRAVE_KEY_ENV}, or run: local-agent key set "
                         f"{BRAVE_KEY_NAME}. To search without a key, run: local-agent web-search duckduckgo")
    if any(not 33 <= ord(char) < 127 for char in key):
        raise ValueError(f"Brave Search has a malformed API key. Check {BRAVE_KEY_ENV} or run: "
                         f"local-agent key set {BRAVE_KEY_NAME}")
    if urlsplit(BRAVE_URL).scheme != "https" and not allow_private:
        raise ValueError("Brave Search must be reached over HTTPS.")
    url = BRAVE_URL + "?" + urlencode({"q": query, "count": max(1, min(limit, 20))})
    try:
        _, _, body = fetch(url, allow_private, headers={"Accept": "application/json",
                                                        "X-Subscription-Token": key},
                           secret_headers={"X-Subscription-Token"})
    except HTTPStatusError as error:
        if error.code in {401, 403}:
            detail = (f"rejected the API key (HTTP {error.code}). Check {BRAVE_KEY_ENV} or run: "
                      f"local-agent key set {BRAVE_KEY_NAME}")
        elif error.code == 429:
            detail = "rate limit or plan quota reached (HTTP 429). Wait a moment, or check your Brave plan."
        else:
            detail = f"failed with HTTP {error.code}."
        raise ValueError(f"Brave Search {detail}") from None
    except PrivateWebAddress:
        raise ValueError("Brave Search request blocked. Private, local and reserved addresses are unavailable.") from None
    except ValueError:
        raise ValueError("Brave Search could not be reached. Check your network connection and try again.") from None
    try:
        data = json.loads(body)
        entries = (data.get("web") or {}).get("results") or []
        results = [{"title": plain(entry.get("title")), "url": str(entry["url"])[:2000],
                    "snippet": plain(entry.get("description"))}
                   for entry in entries if isinstance(entry, dict)
                   and str(entry.get("url", "")).startswith(("http://", "https://"))]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("Brave Search returned an unexpected response.") from None
    result = {"provider": "Brave Search", "results": results[:limit]}
    if not results:
        result["note"] = "Brave Search found no results."
    return result
