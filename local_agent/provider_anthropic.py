"""Claude through Anthropic's Messages API, speaking OpenAI-style messages to the agent."""
import json

from .provider import HttpProvider, PING, assistant_message, printable, system_text, tool_call

API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192
CACHE = {"type": "ephemeral"}


class AnthropicProvider(HttpProvider):
    wire = "anthropic"
    max_body = 4_000_000  # Tool arguments (a whole file for write_file) arrive in many small events.

    def __init__(self, base_url: str, model: str, api_key: str | None, **options):
        super().__init__(base_url, model, api_key, **options)
        self.url = self.base_url + "/messages"
        self.headers["anthropic-version"] = API_VERSION
        if api_key:
            self.headers["x-api-key"] = api_key

    def models(self):
        return self.list_models(self.base_url + "/models?limit=1000")

    def ping_url(self):
        return self.url

    def ping_payload(self):
        return {"model": self.model, "max_tokens": 16, "messages": PING}

    def payload(self, messages, tools):
        system, rest = system_text(messages)
        body = {"model": self.model, "max_tokens": self.max_tokens or DEFAULT_MAX_TOKENS,
                "messages": convert_messages(rest)}
        if system:
            # The system prompt and tool list repeat every round, so let Anthropic cache them.
            body["system"] = [{"type": "text", "text": system, "cache_control": CACHE}]
        if tools:
            converted = [convert_tool(tool) for tool in tools]
            converted[-1]["cache_control"] = CACHE
            body["tools"] = converted
        return body

    def complete(self, messages, tools, on_text=None):
        self.last_usage = None
        body = self.payload(messages, tools)
        if on_text is not None and self.streaming:
            return self.stream(body, on_text)
        data = self.request_json(self.post(self.url, body))
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise RuntimeError("Invalid reply from the Anthropic endpoint.")
        self.last_usage = usage_of(data.get("usage"))
        return finish(data["content"], data.get("stop_reason"))

    def stream(self, body, on_text):
        blocks, stop, usage = {}, None, {}
        with self.open(self.post(self.url, {**body, "stream": True})) as response:
            try:
                for event, raw in self.events(response):
                    data = json.loads(raw)
                    kind = data.get("type", event)
                    if kind == "message_start":
                        usage.update((data.get("message") or {}).get("usage") or {})
                    elif kind == "content_block_start":
                        block = dict(data["content_block"])
                        if block.get("type") == "tool_use":
                            block["partial"] = ""
                        blocks[data["index"]] = block
                    elif kind == "content_block_delta":
                        block, delta = blocks[data["index"]], data["delta"]
                        if delta.get("type") == "text_delta":
                            block["text"] = block.get("text", "") + delta["text"]
                            on_text("content", delta["text"])
                        elif delta.get("type") == "input_json_delta":
                            block["partial"] += delta.get("partial_json", "")
                        elif delta.get("type") == "thinking_delta":
                            on_text("reasoning", delta.get("thinking", ""))
                    elif kind == "message_delta":
                        stop = (data.get("delta") or {}).get("stop_reason") or stop
                        usage.update(data.get("usage") or {})
                    elif kind == "error":
                        detail = (data.get("error") or {}).get("message") or ""
                        raise RuntimeError("The model endpoint reported an error while streaming."
                                           + (f" {printable(detail)[:240]}" if detail else ""))
                    elif kind == "message_stop":
                        break
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise RuntimeError("The endpoint returned an invalid stream.") from error
        for block in blocks.values():
            if block.get("type") == "tool_use":
                try:
                    block["input"] = json.loads(block.pop("partial") or "{}")
                except ValueError as error:
                    raise RuntimeError("The model sent invalid tool arguments.") from error
        self.last_usage = usage_of(usage)
        return finish([blocks[index] for index in sorted(blocks)], stop)


def finish(blocks, stop_reason):
    if stop_reason == "max_tokens":
        raise RuntimeError("The provider returned an incomplete response. Raise it with setup --max-tokens.")
    text, calls = [], []
    for block in blocks:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        elif block.get("type") == "tool_use":
            calls.append(tool_call(block.get("id"), block.get("name"), block.get("input") or {}))
    return assistant_message("".join(text) or None, calls)


def convert_tool(tool):
    function = tool.get("function", tool)
    schema = function.get("parameters") or {"type": "object", "properties": {}}
    return {"name": function["name"], "description": function.get("description", ""), "input_schema": schema}


def convert_messages(messages):
    """OpenAI-style user/assistant/tool messages to Anthropic's alternating user/assistant turns."""
    turns = []

    def add(role, blocks):
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": list(blocks)})

    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role == "user":
            add("user", [{"type": "text", "text": content if isinstance(content, str) and content else "(empty)"}])
        elif role == "assistant":
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) and content.strip() else []
            for call in message.get("tool_calls") or []:
                try:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                               "input": arguments if isinstance(arguments, dict) else {}})
            add("assistant", blocks or [{"type": "text", "text": "(no reply)"}])
        elif role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": message.get("tool_call_id"),
                          "content": content if isinstance(content, str) else json.dumps(content)}])
    if turns and turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation continues)"}]})
    for turn in turns:  # Tool results must come before any text in a user turn.
        if turn["role"] == "user":
            turn["content"].sort(key=lambda block: block["type"] != "tool_result")
    return turns


def usage_of(usage):
    if not isinstance(usage, dict):
        return None
    prompt = sum(usage.get(key) or 0 for key in
                 ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    completion = usage.get("output_tokens") or 0
    if not prompt and not completion:
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
