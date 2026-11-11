"""Parallel writers in isolated Git worktrees. Standard library only (uses the git command).

Each writer gets its own worktree and branch, ``local-agent/<run>/<agent>``, started from the same
base commit (the project's HEAD when the run starts). The user's working tree, including any
uncommitted changes, is never touched. After the writers finish, their changes are committed on
their branches and merged one by one into ``local-agent/<run>/integration`` in a separate worktree;
files changed by more than one writer are reported, and a merge conflict stops the integration with
every branch kept for inspection. Reviews and revisions then work on the integration worktree.
Nothing reaches the user's branch until they run ``team apply``, which merges the integration
branch only into a clean working tree.

Worktrees live outside the project (next to the key store, in ``local-agent/worktrees``). A worktree
is removed only when everything in it is committed on its branch; branches are kept.
"""
import os
import subprocess
import tempfile
from pathlib import Path

from .projects import normalize_path
from .teams import TeamError

IDENTITY = ["-c", "user.name=Local Agent", "-c", "user.email=local-agent@localhost", "-c", "commit.gpgsign=false"]


def git(*args, cwd, check=True, timeout=120):
    try:
        done = subprocess.run(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise TeamError("Git is not installed or not on PATH.") from None
    except subprocess.TimeoutExpired:
        raise TeamError(f"git {' '.join(args[:2])} took too long.") from None
    if check and done.returncode != 0:
        raise TeamError(f"git {' '.join(args[:3])} failed: {(done.stderr or done.stdout).strip()[:400]}")
    return done


def suitability(folder):
    """None when parallel writers can work in this folder, else the reason they can't."""
    folder = Path(folder)
    try:
        inside = git("rev-parse", "--is-inside-work-tree", cwd=folder, check=False)
    except TeamError as error:
        return str(error)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return "the project folder is not a Git repository."
    top = git("rev-parse", "--show-toplevel", cwd=folder, check=False).stdout.strip()
    if not top or normalize_path(top) != normalize_path(folder):
        return "the project folder is inside a Git repository but is not its top folder."
    if git("rev-parse", "--verify", "--quiet", "HEAD", cwd=folder, check=False).returncode != 0:
        return "the repository has no commits yet."
    return None


def default_root():
    from .providers import key_path
    return Path(key_path()).parent / "worktrees"


class ParallelWorkspace:
    def __init__(self, project_path, run_id, writers, root=None):
        reason = suitability(project_path)
        if reason:
            raise TeamError(f"Parallel writers are unavailable: {reason} Use one writer instead.")
        self.project = Path(project_path)
        self.run_id = run_id
        self.writers = list(writers)
        self.root = Path(root or default_root()) / Path(project_path).name / run_id
        self.base = git("rev-parse", "HEAD", cwd=self.project).stdout.strip()
        self.workers = {}
        self.integration_path = self.root / "integration"
        self.integration_branch = f"local-agent/{run_id}/integration"
        self.integrated = False
        self.conflicts, self.overlaps, self.kept = [], [], []

    def branch(self, agent):
        return f"local-agent/{self.run_id}/{agent}"

    def path(self, agent):
        return self.root / agent

    def create(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for agent in self.writers:
            git("worktree", "add", "-b", self.branch(agent), str(self.path(agent)), self.base, cwd=self.project)
            self.workers[agent] = {"branch": self.branch(agent), "path": str(self.path(agent)), "files": [],
                                   "commits": 0, "kept": True}

    @staticmethod
    def commit_all(path, message):
        git("add", "-A", cwd=path)
        if git("diff", "--cached", "--quiet", cwd=path, check=False).returncode == 0:
            return False
        git(*IDENTITY, "commit", "--no-verify", "-q", "-m", message, cwd=path)
        return True

    def integrate(self):
        """Commit each writer's work, report overlapping files, and merge into the integration branch."""
        touched = {}
        for agent, info in self.workers.items():
            self.commit_all(self.path(agent), f"{agent}: work from team run {self.run_id}")
            files = git("diff", "--name-only", f"{self.base}..{info['branch']}", cwd=self.project).stdout.split()
            info["files"] = files
            info["commits"] = int(git("rev-list", "--count", f"{self.base}..{info['branch']}",
                                      cwd=self.project).stdout.strip() or 0)
            for path in files:
                touched.setdefault(path, []).append(agent)
        self.overlaps = sorted(path for path, agents in touched.items() if len(agents) > 1)
        git("worktree", "add", "-b", self.integration_branch, str(self.integration_path), self.base, cwd=self.project)
        for agent, info in self.workers.items():
            if not info["commits"]:
                continue
            merged = git(*IDENTITY, "merge", "--no-ff", "--no-edit", "-m", f"Integrate {agent} ({self.run_id})",
                         info["branch"], cwd=self.integration_path, check=False)
            if merged.returncode != 0:
                files = git("diff", "--name-only", "--diff-filter=U", cwd=self.integration_path,
                            check=False).stdout.split()
                git("merge", "--abort", cwd=self.integration_path, check=False)
                self.conflicts.append(f"{agent} conflicts in {', '.join(files) or 'the merge'}")
        self.integrated = not self.conflicts
        return self.summary()

    def summary(self):
        return {"base": self.base, "branch": self.integration_branch, "path": str(self.integration_path),
                "workers": self.workers, "overlaps": self.overlaps, "conflicts": self.conflicts,
                "kept": self.kept, "integrated": self.integrated}

    def cleanup(self):
        """Commit what the integration worktree holds, then remove worktrees whose contents are safely
        on their branches. Anything uncommitted or conflicting is kept for inspection."""
        if self.integration_path.is_dir():
            self.commit_all(self.integration_path, f"Revisions from team run {self.run_id}")
        self.kept = []
        for path in [*(self.path(agent) for agent in self.workers), self.integration_path]:
            if not path.is_dir():
                continue
            dirty = git("status", "--porcelain", cwd=path, check=False).stdout.strip()
            if dirty or self.conflicts:
                self.kept.append(str(path))
                continue
            git("worktree", "remove", str(path), cwd=self.project, check=False)
        for info in self.workers.values():
            info["kept"] = info["path"] in self.kept


def apply(project_path, record):
    """Merge a run's integration branch into the project's current branch, only if the working tree
    has no uncommitted changes to tracked files. Never pushes."""
    integration = (record.get("result") or {}).get("integration")
    if not integration:
        raise TeamError("This run has no parallel integration branch.")
    if integration.get("conflicts"):
        raise TeamError("This run's integration stopped on conflicts; resolve them on the kept branches first.")
    branch = integration["branch"]
    if git("rev-parse", "--verify", "--quiet", branch, cwd=project_path, check=False).returncode != 0:
        raise TeamError(f"Branch {branch} no longer exists.")
    if git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=project_path, check=False).returncode == 0:
        raise TeamError("Finish the current Git merge before applying this run.")
    dirty = git("status", "--porcelain", "--untracked-files=no", cwd=project_path).stdout.strip()
    if dirty:
        raise TeamError("Your working tree has uncommitted changes. Commit or stash them, then apply again; "
                        "nothing was changed.")
    # A non-fast-forward merge can overwrite ignored files even with --no-overwrite-ignore.
    # Build the merge on a detached HEAD, then let Git check collisions during the fast-forward.
    head = git("rev-parse", "HEAD", cwd=project_path).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="local-agent-apply-") as directory:
        candidate = Path(directory) / "merge"
        git("worktree", "add", "--detach", str(candidate), head, cwd=project_path)
        try:
            git(*IDENTITY, "merge", "--no-ff", "--no-edit", branch, cwd=candidate)
            commit = git("rev-parse", "HEAD", cwd=candidate).stdout.strip()
            git("merge", "--ff-only", "--no-overwrite-ignore", commit, cwd=project_path)
        finally:
            git("worktree", "remove", "--force", str(candidate), cwd=project_path)
    return f"Merged {branch} into your current branch. Nothing was pushed."


def is_repository(folder):
    return suitability(folder) is None and os.path.isdir(folder)
