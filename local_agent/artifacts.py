"""Versioned generated files and browser previews, isolated from the workspace."""
import base64
import html
import json
import re
import uuid
import webbrowser
from pathlib import Path, PureWindowsPath

from .attachments import image_type


ARTIFACT_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
TEXT_MIME_TYPES = {".html": "text/html", ".htm": "text/html", ".md": "text/markdown",
                   ".csv": "text/csv", ".json": "application/json",
                   ".svg": "image/svg+xml", ".css": "text/css"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}


class ArtifactStore:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    def create(self, name, content):
        self.validate_name(name)
        if not isinstance(content, str) or not content.strip() or len(content) > 24_000:
            raise ValueError("Artifact content must contain between 1 and 24,000 characters.")
        suffix = Path(name).suffix.lower()
        if suffix in IMAGE_TYPES:
            raise ValueError("Use generate_image for image files. create_artifact saves UTF-8 text.")
        if suffix == ".json":
            json.loads(content)
        return self._save(name, content.encode("utf-8"))

    def create_image(self, name, data):
        self.validate_name(name, IMAGE_TYPES)
        mime = image_type(data)
        if IMAGE_TYPES.get(Path(name).suffix.lower()) != mime:
            raise ValueError("The image filename extension must match the generated image format.")
        return self._save(name, data)

    def _save(self, name, data):
        if not data or len(data) > 10_000_000:
            raise ValueError("Artifacts must contain between 1 byte and 10 MB.")
        self.directory.mkdir(parents=True, exist_ok=True)
        identifier = uuid.uuid4().hex
        target = self.directory / f"{identifier}-{name}"
        stream = target.open("xb")
        try:
            with stream:
                stream.write(data)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return self.info(target)

    @staticmethod
    def validate_name(name, types=None):
        if (not re.fullmatch(ARTIFACT_NAME, name)
                or PureWindowsPath(name).is_reserved() or name.endswith(".")):
            raise ValueError("Choose a simple artifact filename without directories.")
        if types is not None and Path(name).suffix.lower() not in types:
            raise ValueError("Supported artifact extensions: " + ", ".join(sorted(types)) + ".")

    @staticmethod
    def info(path):
        identifier, name = path.name.split("-", 1)
        suffix = path.suffix.lower()
        return {"id": identifier, "name": name, "path": str(path), "bytes": path.stat().st_size,
                "mime": IMAGE_TYPES.get(suffix, TEXT_MIME_TYPES.get(suffix, "text/plain"))}

    def path(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Use the artifact ID shown by the artifacts command.")
        matches = list(self.directory.glob(identifier + "-*"))
        if len(matches) != 1 or matches[0].resolve() != matches[0] or not matches[0].is_file():
            raise ValueError("Artifact unavailable.")
        return matches[0]

    def list(self):
        paths = [path for path in self.directory.glob("*-*") if path.is_file() and not path.is_symlink()
                 and re.fullmatch(r"[a-f0-9]{32}-" + ARTIFACT_NAME, path.name)]
        paths.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        return {"artifacts": [self.info(path) for path in paths[:100]], "truncated": len(paths) > 100}

    def read(self, identifier):
        path = self.path(identifier)
        result = self.info(path)
        if path.suffix.lower() not in IMAGE_TYPES:
            with path.open(encoding="utf-8") as stream:
                text = stream.read(12_001)
            result.update(text=text[:12_000], truncated=len(text) > 12_000)
        return result

    def remove(self, identifier):
        self.path(identifier).unlink()

    def preview(self, identifier, *, open_browser=False):
        path = self.path(identifier)
        suffix = path.suffix.lower()
        with path.open("rb") as stream:
            data = stream.read(10_000_001)
        if len(data) > 10_000_000:
            raise ValueError("Artifact exceeds its size limit.")
        if suffix in IMAGE_TYPES:
            document = '<img alt="Generated image" style="max-width:100%" src="data:' + image_type(data)
            document += ";base64," + base64.b64encode(data).decode("ascii") + '">'
        elif suffix in {".html", ".htm", ".svg"}:
            document = data.decode("utf-8")
        else:
            document = "<pre>" + html.escape(data.decode("utf-8")) + "</pre>"
        policy = ("default-src 'none'; style-src 'unsafe-inline'; "
                  "img-src data: blob:; font-src data:; connect-src 'none'; "
                  "form-action 'none'; base-uri 'none'")
        document = '<meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">' + document
        wrapper = ('<!doctype html><meta charset="utf-8"><title>' + html.escape(path.name[33:])
                   + '</title><style>html,body,iframe{width:100%;height:100%;margin:0;border:0}</style>'
                   + '<iframe sandbox referrerpolicy="no-referrer" srcdoc="'
                   + html.escape(document, quote=True) + '"></iframe>')
        previews = self.directory / "previews"
        previews.mkdir(exist_ok=True)
        target = previews / (uuid.uuid4().hex + ".html")
        with target.open("x", encoding="utf-8") as stream:
            stream.write(wrapper)
        if open_browser and not webbrowser.open(target.as_uri()):
            raise RuntimeError(f"Could not open the browser. Preview saved to {target}")
        return target
