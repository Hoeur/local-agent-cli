"""File commands shared by plain chat and the terminal interface."""
from pathlib import Path


FILE_COMMANDS = {"attach", "attachments", "detach", "artifacts", "artifact", "open"}


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

    def detach(self, which):
        """Remove one queued attachment by its number in /attachments, or by file name."""
        names = [path.name for path in self.pending]
        if which.isdigit() and 1 <= int(which) <= len(self.pending):
            return self.pending.pop(int(which) - 1)
        if names.count(which) == 1:
            return self.pending.pop(names.index(which))
        raise ValueError("Choose an attachment number or file name from /attachments." if self.pending
                         else "No files attached.")

    def handle(self, prompt):
        command, _, argument = prompt.partition(" ")
        argument = argument.strip()
        if command == "/attach":
            if not argument:
                return "Usage: /attach PATH"
            if argument[:1] in {"'", '"'} and argument[-1:] == argument[:1]:
                argument = argument[1:-1]
            path = Path(argument).expanduser().resolve(strict=True)
            if not path.is_file():
                raise ValueError("Attachments must be regular files.")
            if len(self.pending) >= 4:
                raise ValueError("Attach at most four files per message.")
            self.pending.append(path)
            return f"Attached {path.name} for the next message."
        if command == "/attachments" and not argument:
            return "\n".join(f"{number}. {path}" for number, path in enumerate(self.pending, 1)) \
                or "No files attached."
        if command == "/detach":
            if not argument:
                self.pending.clear()
                return "Attachments cleared."
            return f"Removed {self.detach(argument).name}; it won't be sent."
        if command == "/artifacts" and not argument:
            return artifact_list(self.state.artifacts)
        if command == "/artifact":
            if not argument:
                return "Usage: /artifact ID"
            return artifact_text(self.state.artifacts, argument)
        if command == "/open":
            if not argument:
                return "Usage: /open ID"
            preview = self.state.artifacts.preview(argument, open_browser=True)
            return f"Opened artifact preview: {preview}"
        if command[1:] in FILE_COMMANDS and command.startswith("/"):
            return f"Usage: {command}"
        return None
