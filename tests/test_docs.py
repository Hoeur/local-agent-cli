"""The Windows command examples in the docs must paste cleanly into Command Prompt and PowerShell."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "docs" / "TUI.md", ROOT / "docs" / "PROJECTS.md", ROOT / "docs" / "TEAMS.md"]


def command_blocks(path):
    """Yield (line number, line) for every line inside a ```powershell, ```bat or ```cmd block."""
    inside = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            inside = not inside and line[3:].strip().lower() in {"powershell", "bat", "cmd"}
            continue
        if inside and line.strip():
            yield number, line


class WindowsCommandTests(unittest.TestCase):
    def test_command_blocks_work_in_command_prompt_and_powershell(self):
        problems = {
            "a # comment is passed as arguments in Command Prompt": re.compile(r"(^|\s)#"),
            "single quotes are not quotes in Command Prompt": re.compile(r"(^|\s)'"),
            "a trailing backtick only continues lines in PowerShell": re.compile(r"`\s*$"),
            "$ expands variables in PowerShell": re.compile(r"\$"),
            "&& and || need PowerShell 7": re.compile(r"&&|\|\|"),
            "%VAR% only expands in Command Prompt": re.compile(r"%\w+%"),
        }
        found = []
        for path in DOCS:
            for number, line in command_blocks(path):
                for reason, pattern in problems.items():
                    if pattern.search(line):
                        found.append(f"{path.name}:{number}: {reason}: {line.strip()}")
        self.assertEqual(found, [])
        self.assertTrue(any(True for _ in command_blocks(DOCS[0])))

    def test_release_install_commands_name_the_same_published_wheel(self):
        readme = DOCS[0].read_text(encoding="utf-8")
        release = re.search(r"/releases/download/v(?P<version>\d+\.\d+\.\d+)/"
                            r"(?P<wheel>local_agent_cli-(?P=version)-py3-none-any\.whl)", readme)
        self.assertIsNotNone(release)
        self.assertIn(f'"{release["wheel"]}[tui]"', readme)

    def test_local_wheel_install_command_names_the_packaged_version(self):
        import tomllib

        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        readme = DOCS[0].read_text(encoding="utf-8")
        self.assertIn(f'"dist/local_agent_cli-{version}-py3-none-any.whl[tui]"', readme)


if __name__ == "__main__":
    unittest.main()
