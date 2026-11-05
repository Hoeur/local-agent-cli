"""Model providers.

Every provider takes and returns OpenAI-style chat messages, so the agent never needs to know which
service it is talking to:

- ChatProvider: the OpenAI chat-completions format (Ollama, DeepSeek, Alibaba Model Studio,
  OpenRouter, most resellers). Defined here.
- AnthropicProvider: Claude's Messages API. See provider_anthropic.py.
- ResponsesProvider: OpenAI's Responses API, needed for Codex models. See provider_responses.py.
"""
import json
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BODY = 1_000_000
MAX_LIST_BODY = 8_000_000  # Hosted model lists (OpenRouter's especially) are large.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
RETRY_CODES = {429, 500, 502, 503, 504, 529}
RETRY_DELAYS = (2, 6)  # Seconds before each retry of a rate-limited or overloaded hosted endpoint.
PING = [{"role": "user", "content": "Reply with the word OK."}]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StreamingUnsupported(Exception):
    """The endpoint rejected a streaming request."""


def printable(text):
    return " ".join("".join(char if char.isprintable() else " " for char in str(text)).split())


def error_detail(error):
    """Return the provider's own explanation from an HTTP error body, safe to print, or ""."""
    try:
        body = error.read(8192).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - the status code alone is still useful
        return ""
    finally:
        error.close()
    message = None
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        inner = data.get("error", data)
        if isinstance(inner, dict):
            message = inner.get("message") or inner.get("msg") or inner.get("type") or inner.get("code")
        elif isinstance(inner, str):
            message = inner
        if not isinstance(message, str):
            message = data.get("message")
    elif body.strip() and not body.lstrip().startswith("<"):  # Skip HTML error pages.
        message = body
    return printable(message)[:240] if isinstance(message, str) else ""


def http_message(code, detail):
    return f"Model endpoint returned HTTP {code}." + (f" {detail}" if detail else "")


def retry_after(headers, default):
    try:
        return min(20.0, max(0.0, float(headers.get("retry-after"))))
    except (TypeError, ValueError):
        return default


class HttpProvider:
    """Shared HTTP plumbing: URL checks, retries, readable errors, server-sent events."""
    wire = "?"
    max_body = MAX_BODY

    def __init__(self, base_url: str, model: str, api_key: str | None, *, max_tokens: int | None = None,
                 timeout: float = 90):
        url = urlsplit(base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or any(
            (url.username, url.password, url.query, url.fragment)
        ):
            raise ValueError("Set AGENT_BASE_URL to an HTTP(S) API base URL without credentials.")
        if api_key and url.scheme == "http" and url.hostname not in LOCAL_HOSTS:
            raise ValueError("Use HTTPS when sending an API key to a remote provider.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        self.opener = build_opener(NoRedirect())
        # Hosted services rate-limit and overload; a local server that fails won't do better on retry.
        self.retries = 0 if url.hostname in LOCAL_HOSTS else len(RETRY_DELAYS)
        # Streaming is used only when a caller asks for text fragments (the terminal UI does).
        self.streaming = True
        self.last_usage = None

    def post(self, url, payload):
        return Request(url, headers=self.headers, data=json.dumps(payload).encode("utf-8"))

    def open(self, request, streaming_fallback=False):
        """Open a request, retrying rate limits and overloads before any of the reply is read."""
        for attempt in range(self.retries + 1):
            try:
                return self.opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                delay = retry_after(error.headers, RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                detail = error_detail(error)
                # "Out of quota" also arrives as 429, and waiting won't fix it.
                hopeless = any(word in detail.lower() for word in ("quota", "insufficient", "balance", "billing"))
                if error.code in RETRY_CODES and attempt < self.retries and not hopeless:
                    time.sleep(delay)
                    continue
                if streaming_fallback and error.code in {400, 404, 415, 422, 501}:
                    raise StreamingUnsupported(detail) from error
                raise RuntimeError(http_message(error.code, detail)) from error
            except (URLError, OSError, HTTPException) as error:  # OSError covers timeouts and resets.
                raise RuntimeError(f"Could not reach the model endpoint: {error or type(error).__name__}") from error
        raise AssertionError("unreachable")

    def request_json(self, request, limit=None):
        limit = limit or self.max_body
        response = self.open(request)
        try:
            with response:
                body = response.read(limit + 1)
        except (URLError, OSError, HTTPException) as error:
            raise RuntimeError(f"The model reply was interrupted: {error or type(error).__name__}") from error
        if len(body) > limit:
            raise RuntimeError(f"Model response exceeded {limit // 1_000_000} MB.")
        try:
            return json.loads(body)
        except ValueError as error:
            raise RuntimeError("The endpoint returned invalid JSON.") from error

    def events(self, response):
        """Yield (event, data) pairs from a server-sent event stream."""
        event, data, received = None, [], 0
        try:
            for raw in response:
                received += len(raw)
                if received > self.max_body:
                    raise RuntimeError(f"Model response exceeded {self.max_body // 1_000_000} MB.")
                line = raw.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data:
                        yield event, "\n".join(data)
                    event, data = None, []
                    continue
                if line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event = value
                elif field == "data":
                    data.append(value)
            if data:
                yield event, "\n".join(data)
        except UnicodeDecodeError as error:
            raise RuntimeError("The endpoint returned an invalid stream.") from error
        except (URLError, OSError, HTTPException) as error:
            raise RuntimeError(f"The model stream was interrupted: {error or type(error).__name__}") from error

    def list_models(self, url):
        data = self.request_json(Request(url, headers=self.headers), MAX_LIST_BODY)
        try:
            models = [entry["id"] for entry in data["data"]]
            if not all(isinstance(model, str) for model in models):
                raise ValueError
            return models
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("The endpoint returned an invalid model list.") from error

    def ping_payload(self):
        return {"model": self.model, "messages": PING}

    def ping_url(self):
        raise NotImplementedError

    def check(self):
        """Return (status, detail), where status is "online", "missing" or "offline"."""
        listed = None
        try:
            listed = self.models()
            if self.model in listed:
                return "online", f"Model {self.model!r} is available."
        except RuntimeError:
            pass
        # Some endpoints have no model list, or don't list every alias they serve: send a tiny message.
        try:
            self.request_json(self.post(self.ping_url(), self.ping_payload()))
        except RuntimeError as error:
            if listed is None:
                return ("missing" if "HTTP 404" in str(error) else "offline"), str(error)
            return "missing", (f"Model {self.model!r} isn't served here. "
                               f"Available: {', '.join(listed[:8]) or 'none'}.")
        return "online", f"Model {self.model!r} answered a test message."


class ChatProvider(HttpProvider):
    """OpenAI chat completions: Ollama, DeepSeek, Alibaba Model Studio, OpenRouter, resellers."""
    wire = "chat"

    def __init__(self, base_url: str, model: str, api_key: str | None, **options):
        super().__init__(base_url, model, api_key, **options)
        self.url = self.base_url + "/chat/completions"
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def models(self):
        return self.list_models(self.base_url + "/models")

    def ping_url(self):
        return self.url

    def complete(self, messages, tools, on_text=None):
        """Return the assistant message for one chat completion.

        on_text(kind, text) is optional. When given and streaming is enabled, the reply is
        requested as a server-sent event stream and each fragment is passed on as it arrives,
        with kind "content" or "reasoning". The returned message is validated the same way
        either way. Providers that reject streaming fall back to a single request.
        """
        self.last_usage = None
        payload = {"model": self.model, "messages": messages}
        if tools:  # Some providers reject an empty tools list.
            payload["tools"] = tools
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if on_text is not None and self.streaming:
            try:
                return self.parse_completion(self.stream_completion(payload, on_text))
            except StreamingUnsupported:
                # Only give up on streaming if a plain request works; otherwise it was a real error.
                result = self.parse_completion(self.request_json(self.post(self.url, payload)))
                self.streaming = False
                return result
        return self.parse_completion(self.request_json(self.post(self.url, payload)))

    def stream_completion(self, payload, on_text):
        """Request a streamed completion and rebuild it as a regular completion object."""
        body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        response = self.open(self.post(self.url, body), streaming_fallback=True)
        content, finish, calls = [], None, {}
        with response:
            kind = response.headers.get("Content-Type", "")
            if "json" in kind and "stream" not in kind:
                # The endpoint ignored stream=True and sent one JSON body.
                data = json.loads(response.read(self.max_body + 1) or b"null")
                self.last_usage = usage_of(data)
                return data
            try:
                for _, data in self.events(response):
                    if data.strip() == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if not isinstance(chunk, dict):
                        raise ValueError
                    if "error" in chunk:
                        detail = chunk["error"].get("message") if isinstance(chunk["error"], dict) else chunk["error"]
                        raise RuntimeError("The model endpoint reported an error while streaming."
                                           + (f" {printable(detail)[:240]}" if detail else ""))
                    self.last_usage = usage_of(chunk) or self.last_usage
                    for choice in chunk.get("choices") or []:
                        finish = choice.get("finish_reason") or finish
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning:
                            on_text("reasoning", reasoning)
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            content.append(text)
                            on_text("content", text)
                        for part in delta.get("tool_calls") or []:
                            entry = calls.setdefault(part.get("index", len(calls)), {
                                "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                            })
                            entry["id"] = part.get("id") or entry["id"]
                            entry["type"] = part.get("type") or entry["type"]
                            function = part.get("function") or {}
                            entry["function"]["name"] += function.get("name") or ""
                            entry["function"]["arguments"] += function.get("arguments") or ""
            except (ValueError, AttributeError, TypeError) as error:
                raise RuntimeError("The endpoint returned an invalid stream.") from error
        message = {"content": "".join(content) or None}
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return {"choices": [{"finish_reason": finish, "message": message}]}

    def parse_completion(self, data):
        self.last_usage = usage_of(data) or self.last_usage
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError
            calls = []
            for index, call in enumerate(message.get("tool_calls") or []):
                name = call["function"]["name"]
                # Some local servers omit call ids or send "" for calls with no arguments.
                call_id = call.get("id") or f"call_{index}"
                arguments = call["function"].get("arguments")
                if arguments in (None, ""):
                    arguments = "{}"
                elif isinstance(arguments, dict):
                    arguments = json.dumps(arguments)
                if call.get("type", "function") != "function" or not all(
                    isinstance(field, str) and field for field in (call_id, name, arguments)
                ):
                    raise ValueError
                calls.append({"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": arguments,
                }})
            if len({call["id"] for call in calls}) != len(calls):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            raise RuntimeError("Invalid chat completion from the model endpoint.") from error
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise RuntimeError("The provider returned an incomplete response.")
        return assistant_message(content, calls)


def assistant_message(content, calls):
    result = {"role": "assistant", "content": content}
    if calls:
        result["tool_calls"] = calls
    return result


def tool_call(call_id, name, arguments):
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments, ensure_ascii=False)
    if not all(isinstance(field, str) and field for field in (call_id, name)) or not isinstance(arguments, str):
        raise RuntimeError("The model endpoint returned an invalid tool call.")
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments or "{}"}}


def system_text(messages):
    """Split OpenAI-style messages into the joined system prompt and the rest."""
    system = [m["content"] for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)]
    return "\n\n".join(system), [m for m in messages if m.get("role") != "system"]


def usage_of(data):
    """Return token counts from a completion or stream chunk, if the provider sent them."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    return {key: value for key, value in counts.items() if isinstance(value, int)} or None
