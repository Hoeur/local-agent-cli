import base64
import binascii
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ChatProvider:
    def __init__(self, base_url: str, model: str, api_key: str | None):
        url = urlsplit(base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or any(
            (url.username, url.password, url.query, url.fragment)
        ):
            raise ValueError("Set AGENT_BASE_URL to an HTTP(S) API base URL without credentials.")
        if api_key and url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Use HTTPS when sending an API key to a remote provider.")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.opener = build_opener(NoRedirect())

    def models(self):
        request = Request(self.url.removesuffix("chat/completions") + "models", headers=self.headers)
        data = self.request_json(request)
        try:
            models = [entry["id"] for entry in data["data"]]
            if not all(isinstance(model, str) for model in models):
                raise ValueError
            return models
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("The endpoint returned an invalid model list.") from error

    def request_json(self, request, *, max_bytes=1_000_000, timeout=90):
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
        except HTTPError as error:
            error.close()
            raise RuntimeError(f"Model endpoint returned HTTP {error.code}.") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"Could not reach the model endpoint: {error}") from error
        if len(body) > max_bytes:
            raise RuntimeError(f"Model response exceeded {max_bytes:,} bytes.")
        try:
            return json.loads(body)
        except ValueError as error:
            raise RuntimeError("The endpoint returned invalid JSON.") from error

    def complete(self, messages, tools):
        request = Request(self.url, headers=self.headers, data=json.dumps({
            "model": self.model, "messages": messages, "tools": tools,
        }).encode("utf-8"))
        data = self.request_json(request)
        return self.validate_completion(data)

    def complete_stream(self, messages, tools, on_token):
        """Consume bounded SSE frames, assembling tool calls before any execution.

        Endpoints that ignore streaming may return JSON. Explicit stream rejection
        falls back to the existing request; broken/partial streams never retry.
        """
        request = Request(self.url, headers=self.headers, data=json.dumps({
            "model": self.model, "messages": messages, "tools": tools, "stream": True,
        }).encode("utf-8"))
        try:
            response = self.opener.open(request, timeout=90)
        except HTTPError as error:
            error.close()
            if error.code in {400, 422}:
                return self.complete(messages, tools)
            raise RuntimeError(f"Model endpoint returned HTTP {error.code}.") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"Could not reach the model endpoint: {error}") from error
        try:
            with response:
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    body = response.read(1_000_001)
                    if len(body) > 1_000_000:
                        raise RuntimeError("Model response exceeded 1 MB.")
                    return self.validate_completion(json.loads(body))
                content, calls, finish = [], {}, None
                size = 0
                data_lines = []
                ended = False
                while True:
                    line = response.readline(65_537)
                    size += len(line)
                    if len(line) > 65_536 or size > 1_000_000:
                        raise RuntimeError("Model stream exceeded its size limit.")
                    if not line:
                        break
                    line = line.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                    if line or not data_lines:
                        continue
                    payload = "\n".join(data_lines)
                    data_lines.clear()
                    if payload == "[DONE]":
                        ended = True
                        break
                    chunk = json.loads(payload)
                    if "error" in chunk:
                        raise RuntimeError("The provider reported a streaming error.")
                    for choice in chunk.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text is not None:
                            if not isinstance(text, str):
                                raise ValueError("Invalid content delta")
                            content.append(text)
                            if text:
                                on_token(text)
                        for part in delta.get("tool_calls") or []:
                            index = part["index"]
                            if not isinstance(index, int) or not 0 <= index < 4:
                                raise ValueError("Invalid tool index")
                            call = calls.setdefault(index, {"id": "", "type": "function", "function": {
                                "name": "", "arguments": "",
                            }})
                            if "id" in part:
                                call["id"] += part["id"]
                            if "type" in part:
                                call["type"] = part["type"]
                            for key in ("name", "arguments"):
                                call["function"][key] += (part.get("function") or {}).get(key, "")
                        if choice.get("finish_reason") is not None:
                            finish = choice["finish_reason"]
                if not ended or finish is None:
                    raise RuntimeError("The model stream ended before completion.")
                return self.validate_completion({"choices": [{"finish_reason": finish, "message": {
                    "content": "".join(content) or None,
                    "tool_calls": [calls[index] for index in sorted(calls)],
                }}]})
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise RuntimeError("Invalid streaming response from the model endpoint.") from error
        except (OSError, TimeoutError) as error:
            raise RuntimeError(f"Model stream interrupted: {error}") from error

    @staticmethod
    def validate_completion(data):
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError
            calls = []
            for call in message.get("tool_calls") or []:
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                if call["type"] != "function" or not all(
                    isinstance(field, str) and field for field in (call["id"], name, arguments)
                ):
                    raise ValueError
                calls.append({"id": call["id"], "type": "function", "function": {
                    "name": name, "arguments": arguments,
                }})
            if len({call["id"] for call in calls}) != len(calls):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            raise RuntimeError("Invalid chat completion from the model endpoint.") from error
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise RuntimeError("The provider returned an incomplete response.")
        result = {"role": "assistant", "content": content}
        if calls:
            result["tool_calls"] = calls
        return result


class ImageProvider(ChatProvider):
    """Optional compatible Images API adapter. Never follows returned image URLs."""
    def __init__(self, base_url, model, api_key):
        super().__init__(base_url, model, api_key)
        self.url = self.url.removesuffix("/chat/completions") + "/images/generations"

    def generate(self, prompt, *, output_format="png"):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            raise ValueError("Image prompts must contain between 1 and 4,000 characters.")
        if output_format not in {"png", "jpeg", "webp", "gif"}:
            raise ValueError("Unsupported image output format.")
        request = Request(self.url, headers=self.headers, data=json.dumps({
            "model": self.model, "prompt": prompt, "n": 1, "output_format": output_format,
        }).encode("utf-8"))
        response = self.request_json(request, max_bytes=14_000_000, timeout=180)
        try:
            encoded = response["data"][0]["b64_json"]
            if not isinstance(encoded, str):
                raise ValueError
            data = base64.b64decode(encoded, validate=True)
            if not data or len(data) > 10_000_000:
                raise ValueError
            from .attachments import image_type
            image_type(data)
            return data
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as error:
            raise RuntimeError("The image endpoint must return a PNG, JPEG, WebP, or GIF in "
                               "data[0].b64_json, at most 10 MB. URL-only responses are unsupported.") from error
