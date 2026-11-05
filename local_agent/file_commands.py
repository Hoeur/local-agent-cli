"""File commands shared by plain chat and the terminal interface."""
from pathlib import Path


FILE_HELP = ("/attach PATH queues an image or UTF-8 text file for the next message.\n"
             "/attachments lists queued files; /detach clears them.\n"
             "/artifacts lists generated files; /artifact ID shows content.\n"
             "/open ID opens an isolated browser preview.")


def artifact_list(store):
    listing = store.list()
    lines = [f"{item['id']}  {item['name']}  ({item['bytes']:,} bytes)" for item in listing["artifacts"]]
    if listing["truncated"]:
        lines.append("Showing the latest 100 artifacts.")
    return "\n".join(lines) or "No artifacts yet. Ask the agent to create a file."


def artifact_text(store, identifier):
    item = store.read(identifier)
    text = f"{item['name']}\nID: {item['id']}\nPath: {item['path']}"
    if "text" in item:
        text += "\n\n" + item["text"] + ("\n[Truncated]" if item["truncated"] else "")
    else:
        text += "\nUse /open with this ID to view the image."
    return text


class FileCommands:
    def __init__(self, state, attachments=()):
        self.state = state
        self.pending = list(attachments)

    def handle(self, prompt):
        command, _, argument = prompt.partition(" ")
        argument = argument.strip()
        if command == "/attach":
            if not argument:
                return "Usage: /attach PATH"
            if argument[:1] in {"'", '"'} and argument[-1:] == argument[:1]:
                argument = argument[1:-1]
            path = Path(argument).expanduser().resolve(strict=True)
            self.state.attachments.prepare([*self.pending, path])
            self.pending.append(path)
            return f"Attached {path.name} for the next message."
        if command == "/attachments" and not argument:
            return "\n".join(str(path) for path in self.pending) or "No files attached."
        if command == "/detach" and not argument:
            self.pending.clear()
            return "Attachments cleared."
        if command == "/artifacts" and not argument:
            return artifact_list(self.state.artifacts)
        if command == "/artifact":
            return artifact_text(self.state.artifacts, argument)
        if command == "/open":
            preview = self.state.artifacts.preview(argument, open_browser=True)
            return f"Opened artifact preview: {preview}"
        return None
