"""A scripted, streaming-capable provider for UI tests and demo screenshots."""
import json
import threading
import time


class StreamingFake:
    """Plays back scripted replies. Text replies are streamed in small chunks when asked to."""
    streaming = True

    def __init__(self, *replies, delay=0.0, models=("demo-model",), usage=True):
        self.replies = list(replies)
        self.delay = delay
        self.model_list = list(models)
        self.usage = usage
        self.requests = []
        self.last_usage = None
        self.release = threading.Event()
        self.release.set()

    def models(self):
        return self.model_list

    def complete(self, messages, tools, on_text=None):
        self.requests.append((messages, tools))
        self.release.wait(10)
        reply = self.replies.pop(0) if self.replies else {"role": "assistant", "content": "(no more replies)"}
        if isinstance(reply, Exception):
            raise reply
        content = reply.get("content") or ""
        if on_text is not None:
            if reply.get("reasoning"):
                for word in reply["reasoning"].split(" "):
                    on_text("reasoning", word + " ")
                    time.sleep(self.delay)
            for index in range(0, len(content), 12):
                on_text("content", content[index:index + 12])
                time.sleep(self.delay)
        self.last_usage = {"prompt_tokens": sum(len(json.dumps(m)) for m in messages) // 4,
                           "completion_tokens": max(1, len(content) // 4)} if self.usage else None
        result = {"role": "assistant", "content": reply.get("content")}
        if reply.get("tool_calls"):
            result["tool_calls"] = reply["tool_calls"]
        return result


def call(tool, call_id="call-1", /, **arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)},
    }]}


def text(content, reasoning=None):
    reply = {"role": "assistant", "content": content}
    if reasoning:
        reply["reasoning"] = reasoning
    return reply
