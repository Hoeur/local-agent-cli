"""Tool schemas and the tools that reach outside the agent's own store.

Tools are organized into four opt-in groups, each enabled by a chat flag:

- workspace (--workspace): list_files, read_file, search_files. Read-only.
- write (--write): write_file and patch. The user approves every change.
- shell (--shell): terminal. The user approves every command; commands that look destructive are
  flagged and always need a person to approve them, even with --yes.
- web (--web): web_search and web_extract. Pages are fetched read-only; private addresses are refused.

Standard library only.
"""
import difflib
import fnmatch
import ipaddress
import json
import locale
import ntpath
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
from http.client import HTTPException
from html.parser import HTMLParser
from itertools import islice
from pathlib import Path, PureWindowsPath
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


# ── Schemas ──────────────────────────────────────────────────────────────────────────────

def text(limit, empty=False):
    return ("string", limit, empty)


def number(low, high):
    return ("integer", low, high)


def tool(summary, required=None, optional=None):
    required, optional = required or {}, optional or {}
    fields = {**required, **optional}
    # Limits are enforced by validate_arguments, so the schema sent with every request stays small:
    # with a 12,000-character budget, each character of schema is one less for the conversation.
    properties = {name: {"type": kind} for name, (kind, _, _) in fields.items()}
    schema = {
        "description": summary,
        "parameters": {"type": "object", "properties": properties, "required": list(required)},
    }
    return schema, (required, optional)


_DEFINITIONS = {
    "read_skill": tool("Load a saved workflow by name.", {"name": text(64)}),
    "todo": tool("Track a task list for multi-step work. Send the whole list, one task per line, each "
                 "starting with [ ] pending, [>] in progress or [x] done. Omit items to view it.",
                 optional={"items": text(4000, empty=True)}),
    "session_search": tool("Search earlier conversations for words.", {"query": text(200)}),
    "create_artifact": tool("Save a generated HTML, Markdown, text, CSV, JSON or SVG artifact.",
                            {"name": text(100), "content": text(24_000)}),
    "list_artifacts": tool("List saved artifacts and their IDs."),
    "read_artifact": tool("Read a saved text artifact or get image metadata by ID.", {"id": text(32)}),
    "generate_image": tool("Generate an image artifact when the user asks for one.",
                           {"name": text(100), "prompt": text(4000)}),
    "remember": tool("Save a user-approved durable preference or fact.", {"name": text(64), "value": text(300)}),
    "forget": tool("Remove a saved memory when the user asks.", {"name": text(64)}),
    "save_skill": tool("Save a user-approved reusable workflow.",
                       {"name": text(64), "description": text(160), "steps": text(2400)}),
    "delete_skill": tool("Remove a saved workflow when the user asks.", {"name": text(64)}),
    "list_files": tool("List visible entries in a workspace directory. Use '.' for the root.", {"path": text(512)}),
    "read_file": tool("Read a UTF-8 text file in the workspace. Each line is shown as 'NUMBER| text'; the "
                      "numbers are for reference only and are not part of the file. Use start_line to page.",
                      {"path": text(512)}, {"start_line": number(1, 10_000_000)}),
    "search_files": tool("Search workspace files. pattern is a regex matched against lines; glob filters "
                         "file names like *.py. Give either or both.",
                         optional={"pattern": text(200), "glob": text(100), "path": text(512)}),
    "write_file": tool("Create a workspace text file, or replace an existing one with new full content. "
                       "Write the file's real text, without read_file's 'NUMBER| ' prefixes.",
                       {"path": text(512), "content": text(200_000, True)}),
    "patch": tool("Replace one exact, unique piece of text in a workspace file. Copy old_text without "
                  "read_file's 'NUMBER| ' prefixes.",
                  {"path": text(512), "old_text": text(20_000), "new_text": text(20_000, True)}),
    "terminal": tool("Run a shell command in the workspace and return its output.",
                     {"command": text(2000)}, {"timeout": number(1, 600)}),
    "web_search": tool("Search the web. Returns titles, links and snippets.", {"query": text(300)}),
    "web_extract": tool("Fetch a web page and return its readable text.", {"url": text(2000)}),
}
TOOLS = {name: schema for name, (schema, _) in _DEFINITIONS.items()}
FIELDS = {name: fields for name, (_, fields) in _DEFINITIONS.items()}

TOOLSETS = {
    "core": ("read_skill", "todo", "session_search"),
    "artifacts": ("create_artifact", "list_artifacts", "read_artifact"),
    "image": ("generate_image",),
    "learn": ("remember", "forget", "save_skill", "delete_skill"),
    "workspace": ("list_files", "read_file", "search_files"),
    "write": ("write_file", "patch"),
    "shell": ("terminal",),
    "web": ("web_search", "web_extract"),
}


def tool_names(learn=False, workspace=False, write=False, shell=False, web=False, image=False):
    """The tools a turn offers, in a stable order."""
    enabled = {"core": True, "artifacts": True, "image": image, "learn": learn,
               "workspace": workspace, "write": write, "shell": shell, "web": web}
    return [name for group, names in TOOLSETS.items() if enabled[group] for name in names]


def validate_arguments(name, args):
    """Check a call's arguments against the declared fields. Returns them with integers converted."""
    required, optional = FIELDS[name]
    fields = {**required, **optional}
    problem = None
    if not isinstance(args, dict):
        problem = "send a JSON object"
    elif unknown := sorted(set(args) - set(fields)):
        problem = "unknown field " + ", ".join(map(repr, unknown))
    elif missing := [key for key in required if key not in args]:
        problem = "missing " + ", ".join(map(repr, missing))
    if problem is None:
        cleaned = {}
        for key, value in args.items():
            kind, first, second = fields[key]
            if kind == "string":
                if not isinstance(value, str) or len(value) > first or (not second and not value.strip()):
                    problem = f"{key!r} must be {'a' if second else 'a non-empty'} string of at most {first:,} characters"
                    break
            else:
                if isinstance(value, str) and value.strip().isdigit():
                    value = int(value)
                if isinstance(value, bool) or not isinstance(value, int) or not first <= value <= second:
                    problem = f"{key!r} must be a whole number from {first:,} to {second:,}"
                    break
            cleaned[key] = value
        else:
            return cleaned
    raise ValueError(f"Tool arguments do not match the declared parameters: {problem}.")


# ── Workspace files ──────────────────────────────────────────────────────────────────────

SKIPPED_DIRECTORIES = {"node_modules", "__pycache__", "venv", "env", "build", "dist", "target"}


def is_reserved_name(part):
    # ntpath.isreserved replaces the deprecated PurePath.is_reserved from Python 3.13.
    check = getattr(ntpath, "isreserved", None)
    return check(part) if check is not None else PureWindowsPath(part).is_reserved()


def shorten(value, limit, note="characters omitted"):
    """Keep the start and the end of long text, which is where errors and summaries usually are."""
    if len(value) <= limit:
        return value, False
    head = limit * 2 // 5
    tail = limit - head - 60
    return f"{value[:head]}\n… [{len(value) - head - tail:,} {note}] …\n{value[-tail:]}", True


class Workspace:
    """A directory the model may read, and with --write, change. Hidden and linked paths are refused."""

    def __init__(self, root: Path, output_chars=2400):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("The workspace must be a directory.")
        self.output_chars = output_chars

    def relative(self, path):
        path = path.replace("\\", "/")
        relative = Path(path)
        if relative.anchor or ":" in path or any(
            part.startswith(".") or is_reserved_name(part) for part in relative.parts
        ):
            raise ValueError("Use a visible workspace-relative path with forward slashes.")
        return relative

    def check_inside(self, target, resolved):
        # normcase lets "readme.md" match README.md on Windows; links still resolve elsewhere.
        if not resolved.is_relative_to(self.root) or os.path.normcase(target) != os.path.normcase(resolved):
            raise ValueError("Linked paths are outside the tool's scope.")

    def resolve(self, path):
        """An existing file or directory inside the workspace."""
        target = self.root / self.relative(path)
        resolved = target.resolve(strict=True)
        self.check_inside(target, resolved)
        return resolved

    def resolve_new(self, path):
        """A file that may not exist yet. Its nearest existing folder must be inside the workspace."""
        relative = self.relative(path)
        if not relative.parts:
            raise ValueError("Choose a file path, not the workspace root.")
        target = self.root / relative
        if target.exists() or target.is_symlink():
            resolved = target.resolve(strict=True)
            self.check_inside(target, resolved)
            if not resolved.is_file():
                raise ValueError("Choose a regular file.")
            return resolved
        existing = target.parent
        while not existing.exists():
            existing = existing.parent
        self.check_inside(existing, existing.resolve(strict=True))
        if not existing.is_dir():
            raise ValueError("A parent of that path is a file.")
        return target

    def label(self, target):
        return target.relative_to(self.root).as_posix()

    def list_files(self, path):
        folder = self.resolve(path)
        entries = (
            entry.name + ("/" if entry.is_dir() else "") for entry in folder.iterdir()
            if not entry.name.startswith(".")
        )
        names = list(islice(entries, 101))
        return {"entries": sorted(names[:100]), "truncated": len(names) > 100}

    def read_text(self, target, limit=5_000_000):
        if not target.is_file():
            raise ValueError("Choose a regular file.")
        if target.stat().st_size > limit:
            raise ValueError(f"The file is larger than {limit // 1_000_000} MB.")
        with target.open(encoding="utf-8", newline="") as stream:
            return stream.read()

    def read_file(self, path, start_line=1):
        target = self.resolve(path)
        lines = self.read_text(target).splitlines()
        total = len(lines)
        if total and start_line > total:
            raise ValueError(f"The file has {total} lines.")
        shown, used, end, cut = [], 0, start_line - 1, False
        for number in range(start_line, total + 1):
            row = f"{number:>4}| {lines[number - 1]}"
            if used + len(row) + 1 > self.output_chars:
                if not shown:  # One very long line: show its start rather than nothing.
                    shown.append(row[:self.output_chars])
                    end, cut = number, True
                break
            shown.append(row)
            used += len(row) + 1
            end = number
        result = {"path": self.label(target), "text": "\n".join(shown), "start_line": start_line,
                  "end_line": end, "total_lines": total, "truncated": cut or end < total}
        if cut:
            result["note"] = f"Line {end} is longer than {self.output_chars:,} characters and was cut."
        if end < total:
            result["next_start_line"] = end + 1
        return result

    def search_files(self, pattern=None, glob=None, path="."):
        if pattern is None and glob is None:
            raise ValueError("Give a pattern, a glob, or both.")
        try:
            regex = re.compile(pattern) if pattern is not None else None
        except re.error as error:
            raise ValueError(f"Invalid regular expression: {error}") from None
        base = self.resolve(path)
        if not base.is_dir():
            raise ValueError("Choose a directory to search.")
        matches, used, scanned, truncated = [], 0, 0, False
        for folder, directories, files in os.walk(base):
            directories[:] = sorted(d for d in directories if not d.startswith(".") and d not in SKIPPED_DIRECTORIES)
            for name in sorted(files):
                if name.startswith(".") or (glob and not fnmatch.fnmatch(name.lower(), glob.lower())):
                    continue
                file = Path(folder) / name
                if file.is_symlink() or is_reserved_name(name):
                    continue
                label = self.label(file)
                scanned += 1
                if scanned > 5000:
                    truncated = True
                    break
                if regex is None:
                    rows = [label]
                else:
                    rows = list(self.grep(file, label, regex))
                for row in rows:
                    if len(matches) >= 60 or used + len(row) > self.output_chars:
                        truncated = True
                        break
                    matches.append(row)
                    used += len(row) + 1
                if truncated:
                    break
            if truncated:
                break
        return {"matches": matches, "truncated": truncated}

    @staticmethod
    def grep(file, label, regex):
        try:
            if file.stat().st_size > 1_000_000:
                return
            data = file.read_bytes()
        except OSError:
            return
        if b"\0" in data[:8192]:
            return  # Binary.
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if regex.search(line):
                yield f"{label}:{number}: {line.strip()[:200]}"

    # Changes are planned first so the user can see exactly what will happen, then applied.

    def plan_write(self, path, content):
        target = self.resolve_new(path)
        before = self.read_text(target) if target.exists() else None
        content, stripped = strip_line_numbers(content)
        plan = {"target": target, "content": content, "before": before, "existed": before is not None,
                "diff": diff(before or "", content, self.label(target), new_file=before is None)}
        if stripped:
            plan["note"] = STRIPPED_NOTE
        return plan

    def plan_patch(self, path, old_text, new_text):
        target = self.resolve(path)
        before = self.read_text(target)
        stripped = False
        if before.count(old_text) == 0:  # The model may have copied read_file's line-number prefixes.
            old_text, stripped = strip_line_numbers(old_text)
            if stripped:
                new_text, _ = strip_line_numbers(new_text)
        old, new = old_text, new_text
        count = before.count(old)
        if count == 0 and "\r\n" in before:  # The model usually writes \n; match Windows line endings too.
            old = old_text.replace("\r\n", "\n").replace("\n", "\r\n")
            new = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
            count = before.count(old)
        if count == 0:
            raise ValueError("old_text was not found. Read the file again and copy the text exactly.")
        if count > 1:
            raise ValueError(f"old_text appears {count} times. Include more surrounding lines so it is unique.")
        after = before.replace(old, new, 1)
        plan = {"target": target, "content": after, "before": before, "existed": True,
                "diff": diff(before, after, self.label(target))}
        if stripped:
            plan["note"] = STRIPPED_NOTE
        return plan

    def apply(self, plan):
        path = self.label(plan["target"])
        target = self.resolve_new(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=target.parent,
                                             prefix=".agent-", delete=False)
        temporary = Path(stream.name)
        try:
            with stream:
                stream.write(plan["content"])
            target = self.resolve_new(path)
            current = self.read_text(target) if target.exists() else None
            if current != plan["before"]:
                raise ValueError("The file changed after the diff was prepared. Read it and request approval again.")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        result = {"written": path, "created": not plan["existed"],
                  "characters": len(plan["content"])}
        if plan.get("note"):
            result["note"] = plan["note"]
        return result


NUMBERED_LINE = re.compile(r" *(\d+)\|(?: |$)")
STRIPPED_NOTE = "Removed the 'NUMBER| ' prefixes copied from read_file; they are not part of the file."


def strip_line_numbers(text):
    """Undo read_file's '  12| ' prefixes when a model copies them into content it writes.

    Only text where every line carries a prefix with consecutive numbers is changed, so ordinary
    content (a Markdown table, '1| a' on one line among others) is left alone. Returns (text, changed).
    """
    lines = text.split("\n")
    body = lines[:-1] if lines and lines[-1].strip() == "" else lines  # A trailing newline is fine.
    if not body:
        return text, False
    matches = [NUMBERED_LINE.match(line.rstrip("\r")) for line in body]
    if not all(matches):
        return text, False
    numbers = [int(match.group(1)) for match in matches]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return text, False
    if len(body) == 1 and not body[0].startswith(" "):  # One line like "3| x" could be real content.
        return text, False
    cleaned = [line[match.end():] for line, match in zip(body, matches)]
    return "\n".join(cleaned + lines[len(body):]), True


def diff(before, after, label, new_file=False, limit=120):
    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(), "/dev/null" if new_file else f"a/{label}", f"b/{label}",
        lineterm="", n=2,
    ))
    if len(lines) > limit:
        lines = lines[:limit] + [f"… {len(lines) - limit} more diff lines"]
    return "\n".join(lines) or "(no changes)"


# ── Terminal ─────────────────────────────────────────────────────────────────────────────

# Commands matching these are flagged as dangerous. A flagged command always needs a person to approve
# it, even with --yes, and is refused when nobody can be asked. This is a safety net for mistakes, not
# a sandbox: approve only commands you understand.
DANGEROUS = [
    (r"\brm\s+(-\S*[rf]|--recursive|--force)", "deletes files recursively or forcibly"),
    (r"\b(rmdir|rd)\s.*\/s\b", "deletes a directory tree"),
    (r"\b(del|erase)\s.*\/[sq]\b", "deletes files in bulk"),
    (r"\b(remove-item|rm|del|rmdir)\b.*\s-r(ecurse)?\b", "deletes a directory tree"),
    (r"\b(mkfs|diskpart|fdisk|parted|wipefs)\b|\bformat(\.com)?\s+[a-z]:", "formats or repartitions a disk"),
    (r"\bdd\s.*\bof=", "writes raw data to a device or file"),
    (r">\s*/dev/(sd|nvme|hd|disk)", "writes to a raw disk"),
    (r"\b(shutdown|reboot|halt|poweroff|restart-computer|stop-computer)\b", "shuts down or restarts the computer"),
    (r"\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b.*\|\s*(sudo\s+)?"
     r"(sh|bash|zsh|python\d*|iex|invoke-expression|powershell|pwsh)\b", "runs a downloaded script"),
    (r"\b(iex|invoke-expression)\b", "runs generated code"),
    (r"\bgit\s+push\b.*(--force|\s-f\b)|\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-\S*f",
     "rewrites or discards git history or files"),
    (r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", "deletes database data"),
    (r"\b(chmod|chown|icacls|takeown)\b.*(\s-\S*R\b|--recursive|\/t\b)", "changes permissions recursively"),
    (r"\bsudo\b|\brunas\b|\bstart-process\b.*-verb\s+runas", "runs with administrator rights"),
    (r"\breg\s+(delete|add)\b|\bbcdedit\b|\bvssadmin\b|\bcipher\s+\/w\b|\bsc(\.exe)?\s+(delete|config|stop)\b",
     "changes system configuration"),
    (r"\b(kill|pkill|killall)\s+-(9|kill)\b|\btaskkill\b.*\/f\b|\bstop-process\b", "force-stops processes"),
    (r":\(\)\s*\{.*:\|:", "starts a fork bomb"),
    (r"\bsetx\b|\[environment\]::setenvironmentvariable", "changes environment variables permanently"),
    (r"\bcrontab\s+-r\b|\bschtasks\b.*\/delete\b", "deletes scheduled tasks"),
]
_DANGEROUS = [(re.compile(pattern, re.IGNORECASE | re.DOTALL), reason) for pattern, reason in DANGEROUS]


def dangerous_reason(command):
    """Why a command looks destructive, or None."""
    flat = " ".join(command.split())
    for pattern, reason in _DANGEROUS:
        if pattern.search(flat):
            return reason
    return None


def shell_name():
    return os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh"


def kill_tree(process):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "latin-1", "replace")


def run_command(command, cwd, timeout=60, output_chars=2400, poll=None):
    """Run command through the system shell with no input. poll() is called while it runs and may
    raise to stop it (the terminal UI uses this to cancel). The whole process tree is stopped on a
    timeout or a cancellation."""
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, **options)
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while True:
            try:
                output, _ = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if poll is not None:
                    poll()
                if time.monotonic() > deadline:
                    timed_out = True
                    kill_tree(process)
                    output, _ = process.communicate(timeout=10)
                    break
    except BaseException:
        kill_tree(process)
        process.communicate()
        raise
    text_output, truncated = shorten(decode(output or b"").replace("\r\n", "\n"), output_chars)
    result = {"exit_code": process.returncode, "output": text_output, "truncated": truncated}
    if timed_out:
        result["timed_out"] = True
        result["note"] = f"Stopped after {timeout} seconds."
    return result


# ── Web ──────────────────────────────────────────────────────────────────────────────────

USER_AGENT = "Mozilla/5.0 (compatible; local-agent/0.3)"
NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
IPV4_PROTOCOL_ASSIGNMENTS = ipaddress.IPv4Network("192.0.0.0/24")
PUBLIC_IPV4_ASSIGNMENTS = {ipaddress.IPv4Address("192.0.0.9"), ipaddress.IPv4Address("192.0.0.10")}
IPV6_DOCUMENTATION = ipaddress.IPv6Network("3fff::/20")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def is_public_address(address):
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.sixtofour is not None or ip in IPV6_DOCUMENTATION:
            return False
        if ip in NAT64_PREFIX:
            ip = ipaddress.IPv4Address(ip.packed[-4:])
        else:
            ip = ip.ipv4_mapped or ip
    # Older supported Python versions classify some IANA protocol addresses as public.
    if ip in IPV4_PROTOCOL_ASSIGNMENTS:
        return ip in PUBLIC_IPV4_ASSIGNMENTS
    return ip.is_global and not (ip.is_reserved or ip.is_multicast or getattr(ip, "is_site_local", False))


class PrivateWebAddress(ValueError):
    """A web destination resolved to an address outside the public internet."""


def connect_public(destination, timeout, source_address=None):
    host, port = destination
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise ValueError(f"Could not resolve {host}.") from None
    if any(not is_public_address(address[0]) for _, _, _, _, address in addresses):
        raise PrivateWebAddress("Private, local and reserved network addresses are not fetched.")
    # Connect directly to the checked socket address. A proxy or another DNS lookup could pick a different peer.
    failure = OSError("No usable address was found.")
    for family, kind, protocol, _, address in addresses:
        try:
            peer = socket.socket(family, kind, protocol)
            try:
                peer.settimeout(timeout)
                if source_address:
                    peer.bind(source_address)
                peer.connect(address)
            except BaseException:
                peer.close()
                raise
            return peer
        except OSError as error:
            failure = error
    raise failure


class PublicWebHandler(HTTPHandler, HTTPSHandler):
    def do_open(self, factory, request, **kwargs):
        def pinned_connection(host, **options):
            connection = factory(host, **options)
            # Keep the original HTTP host and TLS verification, and replace only socket creation.
            connection._create_connection = connect_public
            return connection

        return super().do_open(pinned_connection, request, **kwargs)


class HTTPStatusError(ValueError):
    """The site answered with an error status. code holds it, for callers that explain it."""

    def __init__(self, code):
        super().__init__(f"The site returned HTTP {code}.")
        self.code = code


def fetch(url, allow_private=False, limit=2_000_000, timeout=20, *, user_agent=USER_AGENT, headers=None,
          secret_headers=()):
    """GET a URL, following up to five redirects and checking each hop. Returns (url, type, bytes).
    secret_headers (an API key, say) are sent only to the first URL's origin, never after a redirect
    to another one."""
    handlers = () if allow_private else (ProxyHandler({}), PublicWebHandler())
    opener = build_opener(NoRedirect(), *handlers)
    headers = {"User-Agent": user_agent, "Accept": "text/html,text/plain,*/*;q=0.5", **(headers or {})}
    origin = urlsplit(url)[:2] + (urlsplit(url).port,)
    for _ in range(6):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            raise ValueError("Use an http or https URL without credentials.")
        if (parts.scheme, parts.netloc, parts.port) != origin:
            headers = {key: value for key, value in headers.items() if key not in secret_headers}
        request = Request(url, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(limit + 1)
                kind = response.headers.get("Content-Type", "")
                charset = response.headers.get_content_charset() or "utf-8"
                break
        except HTTPError as error:
            location = error.headers.get("Location") if error.code in {301, 302, 303, 307, 308} else None
            error.close()
            if location:
                url = urljoin(url, location)
                continue
            raise HTTPStatusError(error.code) from None
        except (URLError, OSError, HTTPException) as error:
            raise ValueError(f"Could not fetch the page: {getattr(error, 'reason', error)}") from None
    else:
        raise ValueError("Too many redirects.")
    if len(body) > limit:
        body = body[:limit]
    return url, kind.lower(), body.decode(charset, "replace")


class TextExtractor(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
              "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "main", "nav", "form", "dd", "dt"}
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.skipping, self.in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        if tag == "title":
            self.in_title = True
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        if tag == "title":
            self.in_title = False
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skipping:
            self.parts.append(data)

    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def web_extract(url, output_chars=2400, allow_private=False):
    final, kind, body = fetch(url, allow_private)
    title = ""
    if "html" in kind or body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        parser = TextExtractor()
        parser.feed(body)
        title, content = " ".join(parser.title.split()), parser.text()
    elif kind.startswith("text/") or "json" in kind or "xml" in kind or not kind:
        content = body
    else:
        raise ValueError(f"Can't read {kind.split(';')[0]} content as text.")
    content, truncated = shorten(content, output_chars)
    return {"url": final, "title": title, "text": content, "truncated": truncated}


class SearchResults(HTMLParser):
    """Reads DuckDuckGo's HTML results page: result__a links and result__snippet text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results, self.field = [], None

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            href = dict(attrs).get("href") or ""
            target = parse_qs(urlsplit(href).query).get("uddg", [href])[0]
            if target.startswith("//"):
                target = "https:" + target
            self.results.append({"title": "", "url": target, "snippet": ""})
            self.field = "title"
        elif "result__snippet" in classes and self.results:
            self.field = "snippet"

    def handle_endtag(self, tag):
        if tag in {"a", "td", "div"}:
            self.field = None

    def handle_data(self, data):
        if self.field and self.results:
            self.results[-1][self.field] += data


SEARCH_URL = "https://html.duckduckgo.com/html/"
WIKIPEDIA_SEARCH_URL = "https://en.wikipedia.org/w/rest.php/v1/search/page"


def wikipedia_search(query, limit):
    url = WIKIPEDIA_SEARCH_URL + "?" + urlencode({"q": query, "limit": limit})
    _, _, body = fetch(url, user_agent="local-agent-cli/0.3 (https://github.com/Hoeur/local-agent-cli)")
    try:
        pages = json.loads(body)["pages"]
        return [{"title": page["title"][:300], "url": "https://en.wikipedia.org/wiki/" + quote(page["key"], safe=""),
                 "snippet": (page.get("description") or "")[:300]} for page in pages[:limit]]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Wikipedia search returned an unexpected response.") from error


def web_search(query, limit=6, search_url=None, allow_private=False, provider="duckduckgo"):
    """Search with the chosen provider (see search.py). Every result names its provider."""
    from . import search

    if search.check_provider(provider) == "brave":
        return search.brave_search(query, limit, allow_private)
    result = duckduckgo_search(query, limit, search_url, allow_private)
    return {"provider": result.pop("provider", None) or search.label(provider, search_url), **result}


def duckduckgo_search(query, limit=6, search_url=None, allow_private=False):
    endpoint = search_url or os.environ.get("AGENT_SEARCH_URL") or SEARCH_URL
    query_string = urlencode({"q": query})
    separator = "&" if "?" in endpoint else "?"
    _, _, body = fetch(endpoint + separator + query_string, allow_private)
    if endpoint == SEARCH_URL and 'id="challenge-form"' in body:
        return {"provider": "English Wikipedia (DuckDuckGo fallback)", "results": wikipedia_search(query, limit),
                "note": "DuckDuckGo blocked automated search. Results are limited to English Wikipedia."}
    parser = SearchResults()
    parser.feed(body)
    results = []
    for entry in parser.results:
        if entry["url"].startswith(("http://", "https://")) and "duckduckgo.com/y.js" not in entry["url"]:
            results.append({key: " ".join(value.split())[:300] for key, value in entry.items()})
        if len(results) >= limit:
            break
    if not results:
        return {"results": [], "note": "No results were found, or the search page could not be read."}
    return {"results": results}


# ── Todo ─────────────────────────────────────────────────────────────────────────────────

TODO_LIMIT = 30
TODO_MARKS = {" ": "pending", "": "pending", ">": "in progress", "~": "in progress", "-": "in progress",
              "x": "done", "X": "done", "✓": "done"}


def parse_todo(items):
    """Normalize a task list to lines like '[ ] task', '[>] task' and '[x] task'."""
    tasks = []
    for raw in items.splitlines():
        line = raw.strip()
        line = re.sub(r"^([-*•]|\d+[.)])\s+", "", line)
        if not line:
            continue
        match = re.match(r"^\[(.?)\]\s*(.*)$", line)
        mark, body = (match.group(1), match.group(2)) if match else (" ", line)
        status = TODO_MARKS.get(mark, "pending")
        if body.strip():
            tasks.append("[" + {"pending": " ", "in progress": ">", "done": "x"}[status] + "] " + body.strip()[:200])
    if len(tasks) > TODO_LIMIT:
        raise ValueError(f"Keep the task list to {TODO_LIMIT} items or fewer.")
    return tasks


TODO_ACTIONS = ("add", "edit", "move", "start", "done", "undo", "remove")
TODO_USAGE = ("/todo add TEXT · /todo edit N TEXT · /todo move N TO · /todo start N · /todo done N · "
              "/todo undo N · /todo remove N")


def edit_todo(tasks, command):
    """Apply one manual edit like 'add TEXT', 'edit 2 TEXT' or 'move 3 1' to a normalized task list.
    Returns (new list, short description). Numbers count from 1, as /todo shows them."""
    action, _, rest = command.strip().partition(" ")
    action, rest = action.lower(), rest.strip()
    tasks = list(tasks)
    if action not in TODO_ACTIONS:
        raise ValueError("Usage: " + TODO_USAGE)

    def body(value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("Give the task text.")
        if len(value) > 200:
            raise ValueError("Keep each task to 200 characters or fewer.")
        return value

    if action == "add":
        if len(tasks) >= TODO_LIMIT:
            raise ValueError(f"Keep the task list to {TODO_LIMIT} items or fewer.")
        tasks.append("[ ] " + body(rest))
        return tasks, f"Added task {len(tasks)}."
    number, _, rest = rest.partition(" ")
    if not number.isdigit() or not 1 <= int(number) <= len(tasks):
        raise ValueError(f"Choose a task number from 1 to {len(tasks)}." if tasks else "The task list is empty.")
    index = int(number) - 1
    if action == "edit":
        tasks[index] = tasks[index][:4] + body(rest)
        return tasks, f"Edited task {number}."
    if action == "move":
        target = rest.strip()
        if not target.isdigit() or not 1 <= int(target) <= len(tasks):
            raise ValueError(f"Move it to a position from 1 to {len(tasks)}.")
        tasks.insert(int(target) - 1, tasks.pop(index))
        return tasks, f"Moved task {number} to {target}."
    if rest.strip():
        raise ValueError("Usage: " + TODO_USAGE)
    if action == "remove":
        tasks.pop(index)
        return tasks, f"Removed task {number}."
    mark = {"start": ">", "done": "x", "undo": " "}[action]
    tasks[index] = f"[{mark}]" + tasks[index][3:]
    return tasks, {"start": f"Started task {number}.", "done": f"Completed task {number}.",
                   "undo": f"Marked task {number} pending."}[action]


def todo_summary(tasks):
    done = sum(task.startswith("[x]") for task in tasks)
    return {"todos": tasks, "summary": f"{done}/{len(tasks)} done" if tasks else "No tasks."}


def as_json(value):
    return json.dumps(value, ensure_ascii=False)
