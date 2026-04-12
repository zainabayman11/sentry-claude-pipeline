import subprocess
from pathlib import Path


def run_git_command(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def ensure_git_repo(repo_path: str) -> None:
    subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


def create_worktree(repo_path: str, branch_name: str) -> str:
    ensure_git_repo(repo_path)

    # Worktree must live inside the repo so git path resolution is consistent
    worktrees_root = Path(repo_path) / "worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)

    worktree_path = worktrees_root / branch_name

    if worktree_path.exists():
        return str(worktree_path.resolve())

    # If branch already exists, attach to it — don't create with -b
    branch_check = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=repo_path, capture_output=True, text=True,
    )
    branch_exists = bool(branch_check.stdout.strip())

    if branch_exists:
        run_git_command(
            ["git", "worktree", "add", str(worktree_path), branch_name],
            cwd=repo_path,
        )
    else:
        run_git_command(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"],
            cwd=repo_path,
        )

    return str(worktree_path.resolve())


def commit_and_push_worktree(worktree_path: str, branch_name: str, commit_message: str) -> None:
    """
    Stage all changes, commit if staged changes exist, and push to remote.
    Uses run_git_command consistently for all git operations.
    """
    # Stage all changes
    run_git_command(["git", "add", "."], cwd=worktree_path)

    # Check if there are staged changes (returncode: 0=no changes, 1=changes exist, other=error)
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=worktree_path,
        capture_output=True,
    )
    if status.returncode == 1:  # 1 means changes are staged
        run_git_command(["git", "commit", "-m", commit_message], cwd=worktree_path)
    elif status.returncode not in (0, 1):
        raise RuntimeError(f"git diff failed with code {status.returncode}")

    # Push to remote
    run_git_command(["git", "push", "-u", "origin", branch_name], cwd=worktree_path)


def create_github_pr(worktree_path: str, title: str, body_file: str, base: str = "main") -> None:
    """
    Create a GitHub pull request using the gh CLI.
    """
    subprocess.run(
        ["gh", "pr", "create", "--base", base, "--title", title, "--body-file", body_file],
        cwd=worktree_path,
        check=True,
    )