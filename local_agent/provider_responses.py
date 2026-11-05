"""OpenAI's Responses API, which Codex models require, speaking OpenAI-style chat messages to the agent."""
import json

from .provider import HttpProvider, PING, assistant_message, printable, system_text, tool_call


class ResponsesProvider(HttpProvider):
    wire = "responses"
    max_body = 4_000_000

    def __init__(self, base_url: str, model: str, api_key: str | None, **options):
        super().__init__(base_url, model, api_key, **options)
        self.url = self.base_url + "/responses"
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def models(self):
        return self.list_models(self.base_url + "/models")

    def ping_url(self):
        return self.url

    def ping_payload(self):
        return {"model": self.model, "input": PING[0]["content"], "max_output_tokens": 16, "store": False}

    def payload(self, messages, tools):
        system, rest = system_text(messages)
        # store=False: the conversation lives in this app's own database, not on OpenAI's servers.
        body = {"model": self.model, "input": convert_messages(rest), "store": False}
        if system:
            body["instructions"] = system
        if tools:
            body["tools"] = [convert_tool(tool) for tool in tools]
        if self.max_tokens:
            body["max_output_tokens"] = self.max_tokens
        return body

    def complete(self, messages, tools, on_text=None):
        self.last_usage = None
        body = self.payload(messages, tools)
        if on_text is not None and self.streaming:
            return self.stream(body, on_text)
        return self.parse(self.request_json(self.post(self.url, body)))

    def stream(self, body, on_text):
        final = None
        with self.open(self.post(self.url, {**body, "stream": True})) as response:
            try:
                for event, raw in self.events(response):
                    if raw.strip() == "[DONE]":
                        break
                    data = json.loads(raw)
                    kind = data.get("type", event)
                    if kind == "response.output_text.delta":
                        on_text("content", data.get("delta", ""))
                    elif kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                        on_text("reasoning", data.get("delta", ""))
                    elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                        final = data.get("response")
                        break
                    elif kind == "error":
                        detail = data.get("message") or (data.get("error") or {}).get("message") or ""
                        raise RuntimeError("The model endpoint reported an error while streaming."
                                           + (f" {printable(detail)[:240]}" if detail else ""))
            except (ValueError, TypeError, AttributeError) as error:
                raise RuntimeError("The endpoint returned an invalid stream.") from error
        if final is None:
            raise RuntimeError("The model stream ended before the response was complete.")
        # The final event carries the whole response, so tool calls are taken from it, not the deltas.
        return self.parse(final)

    def parse(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("output"), list):
            raise RuntimeError("Invalid reply from the Responses endpoint.")
        self.last_usage = usage_of(data.get("usage"))
        status = data.get("status")
        if status == "failed":
            detail = (data.get("error") or {}).get("message") or ""
            raise RuntimeError("The model endpoint reported an error." + (f" {printable(detail)[:240]}" if detail else ""))
        if status == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason") or "unknown reason"
            raise RuntimeError(f"The provider returned an incomplete response ({reason}).")
        text, calls = [], []
        for item in data["output"]:
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        text.append(part["text"])
            elif item.get("type") == "function_call":
                calls.append(tool_call(item.get("call_id"), item.get("name"), item.get("arguments")))
        return assistant_message("".join(text) or None, calls)


def convert_tool(tool):
    function = tool.get("function", tool)
    return {"type": "function", "name": function["name"], "description": function.get("description", ""),
            "parameters": function.get("parameters") or {"type": "object", "properties": {}}, "strict": False}


def convert_messages(messages):
    items = []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            items.append({"role": role, "content": content})
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                items.append({"type": "function_call", "call_id": call["id"], "name": call["function"]["name"],
                              "arguments": call["function"].get("arguments") or "{}"})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message.get("tool_call_id"),
                          "output": content if isinstance(content, str) else json.dumps(content)})
    return items


def usage_of(usage):
    if not isinstance(usage, dict):
        return None
    counts = {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
              "total_tokens": usage.get("total_tokens")}
    return {key: value for key, value in counts.items() if isinstance(value, int)} or None
