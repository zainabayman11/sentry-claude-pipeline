import subprocess
from pathlib import Path


def run_git_command(args: list[str], cwd: str) -> None:
    """
    Executes git commands via subprocess.
    If the command fails, subprocess will raise a CalledProcessError.
    """
    subprocess.run(args, cwd=cwd, check=True)


def ensure_git_repo(repo_path: str) -> None:
    """
    Verifies that the provided path is a valid git repository.
    Raises an error if the path is not inside a git work tree.
    """
    subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


def create_worktree(repo_path: str, branch_name: str) -> str:
    """
    Creates a new git worktree on a new branch.
    Command: git worktree add -b <branch> <path> HEAD
    Returns the absolute path to the newly created worktree.
    """
    ensure_git_repo(repo_path)

    worktrees_root = Path("worktrees")
    worktrees_root.mkdir(parents=True, exist_ok=True)

    worktree_path = worktrees_root / branch_name

    # Check if the worktree directory already exists to prevent crashes
    if worktree_path.exists():
        return str(worktree_path.resolve())

    run_git_command(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"],
        cwd=repo_path,
    )

    return str(worktree_path.resolve())