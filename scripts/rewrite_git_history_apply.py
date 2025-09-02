#!/usr/bin/env python3
"""Apply commit message rewrites to all branches, maintaining shared history.

This script rewrites all branches that contain commits to be rewritten,
ensuring they all share the same rewritten history for common commits.

Usage:
    python scripts/rewrite_git_history_apply.py --dry-run
    python scripts/rewrite_git_history_apply.py
"""

import argparse
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMIT_DIR = REPO_ROOT / "commit_messages"


def load_rewrites() -> dict:
    """Load all rewrite JSON files."""
    rewrites = {}

    for json_file in COMMIT_DIR.glob("*.json"):
        with json_file.open() as f:
            data = json.load(f)
        if data["action"] == "rewrote":
            rewrites[data["commit_hash"]] = data["commit_message"]

    return rewrites


def get_all_branches() -> list[str]:
    """Get all local branch names."""
    result = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip().split("\n")


def get_commits_for_branch(branch: str) -> list[str] | None:
    """Get all commits for a specific branch. Returns None if branch doesn't exist."""
    # Try the branch as-is first
    result = subprocess.run(
        ["git", "rev-list", "--reverse", branch],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip().split("\n")

    # Try with origin/ prefix if the direct branch failed
    result = subprocess.run(
        ["git", "rev-list", "--reverse", f"origin/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip().split("\n")

    return None


def rewrite_commit(commit: str, rewrites: dict, commit_map: dict) -> str:
    """Rewrite a single commit, using cached result if available."""
    # If we've already rewritten this commit, return the cached result
    if commit in commit_map:
        return commit_map[commit]

    # Get the commit's tree and parents
    result = subprocess.run(
        ["git", "cat-file", "-p", commit],
        capture_output=True,
        text=True,
        check=True,
    )

    # Parse commit object
    lines = result.stdout.split("\n")
    tree = None
    parents = []
    for line in lines:
        if line.startswith("tree "):
            tree = line.split()[1]
        elif line.startswith("parent "):
            parent_hash = line.split()[1]
            # Recursively rewrite parent if needed
            new_parent = rewrite_commit(parent_hash, rewrites, commit_map)
            parents.append(new_parent)
        elif line == "":
            break

    # Check if this commit needs rewriting or if any parent was rewritten
    needs_new_commit = commit in rewrites or any(
        p != orig for p, orig in zip(parents, [line.split()[1] for line in lines if line.startswith("parent ")])
    )

    if not needs_new_commit:
        # No changes needed, use original commit
        commit_map[commit] = commit
        return commit

    # Get author and committer info
    result = subprocess.run(
        ["git", "show", "-s", "--format=%an|%ae|%at|%cn|%ce|%ct", commit],
        capture_output=True,
        text=True,
        check=True,
    )
    author_name, author_email, author_date, committer_name, committer_email, committer_date = (
        result.stdout.strip().split("|")
    )

    # Get the commit message (original or rewritten)
    if commit in rewrites:
        message = rewrites[commit]
        if not message.endswith("\n"):
            message += "\n"
    else:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%B", commit],
            capture_output=True,
            text=True,
            check=True,
        )
        message = result.stdout

    # Create new commit
    env = {
        **subprocess.os.environ,
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": f"@{author_date}",
        "GIT_COMMITTER_NAME": committer_name,
        "GIT_COMMITTER_EMAIL": committer_email,
        "GIT_COMMITTER_DATE": f"@{committer_date}",
    }

    # Build commit-tree command
    cmd = ["git", "commit-tree", tree]
    for parent in parents:
        cmd.extend(["-p", parent])
    cmd.extend(["-m", message])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    new_commit = result.stdout.strip()
    commit_map[commit] = new_commit

    return new_commit


def main() -> None:  # noqa: C901, PLR0912, PLR0915
    """Apply rewrites to all branches maintaining shared history."""
    parser = argparse.ArgumentParser(description="Apply commit message rewrites to all branches")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be changed")
    parser.add_argument("--branches", nargs="+", help="Specific branches to rewrite (default: all)")
    args = parser.parse_args()

    print("📂 Loading commit rewrites...")
    rewrites = load_rewrites()
    print(f"📊 Found {len(rewrites)} commits to rewrite")

    if not rewrites:
        print("Nothing to do!")
        return

    # Get branches to process
    branches = args.branches or get_all_branches()

    print(f"🌿 Processing {len(branches)} branches: {', '.join(branches)}")

    # Find which branches need rewriting
    branches_to_rewrite = {}
    missing_branches = []
    for branch in branches:
        commits = get_commits_for_branch(branch)
        if commits is None:
            print(f"  ⚠️  {branch}: branch not found (skipping)")
            missing_branches.append(branch)
            continue
        commits_to_rewrite = [c for c in commits if c in rewrites]
        if commits_to_rewrite:
            branches_to_rewrite[branch] = commits[-1]  # Store the branch tip
            print(f"  • {branch}: {len(commits_to_rewrite)} commits to rewrite")

    if not branches_to_rewrite:
        print("✅ No branches need rewriting!")
        return

    if args.dry_run:
        print("\n🔍 DRY RUN - Would rewrite these branches:")
        for branch in branches_to_rewrite:
            print(f"  • {branch}")
        return

    print(f"\n⚠️  This will rewrite {len(branches_to_rewrite)} branches!")
    print("   Original branches will be backed up with -backup suffix")
    response = input("Continue? (y/N): ")
    if response.lower() != "y":
        print("Aborted.")
        return

    # Global commit map shared across all branches
    # This ensures common commits get the same new hash
    commit_map = {}

    # Store current branch to restore later
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    original_branch = result.stdout.strip() if result.returncode == 0 else None

    print("\n🔄 Rewriting branches...")

    for branch, tip_commit in branches_to_rewrite.items():
        print(f"\n📝 Processing branch: {branch}")

        # Create backup
        backup_branch = f"{branch}-backup"
        subprocess.run(
            ["git", "branch", "-f", backup_branch, branch],
            check=True,
            capture_output=True,
        )
        print(f"  💾 Created backup: {backup_branch}")

        # Rewrite the branch tip (this will recursively rewrite all needed commits)
        new_tip = rewrite_commit(tip_commit, rewrites, commit_map)

        # Update the branch to point to the new tip
        # If this is the current branch, we need to use reset instead of branch -f
        if branch == original_branch:
            subprocess.run(
                ["git", "reset", "--hard", new_tip],
                check=True,
                capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "branch", "-f", branch, new_tip],
                check=True,
                capture_output=True,
            )

        print(f"  ✅ Rewrote {branch}: {tip_commit[:8]} → {new_tip[:8]}")

    # Restore original branch
    if original_branch and original_branch in branches_to_rewrite:
        subprocess.run(["git", "checkout", original_branch], check=True, capture_output=True)

    print("\n✅ Successfully rewrote all branches!")
    print("\n📊 Summary:")
    print(f"  • Rewrote {len(branches_to_rewrite)} branches")
    print(f"  • Created {len(commit_map)} new commits")
    print("  • Backups saved with -backup suffix")

    print("\n💡 Next steps:")
    print("  1. Verify the rewritten branches look correct")
    print("  2. Delete backup branches when satisfied:")
    for branch in branches_to_rewrite:
        print(f"     git branch -D {branch}-backup")
    print("  3. Force push to remote:")
    print("     git push --force-with-lease --all")

    print("\n⚠️  To undo if something went wrong:")
    for branch in branches_to_rewrite:
        print(f"  git branch -f {branch} {branch}-backup")


if __name__ == "__main__":
    main()
