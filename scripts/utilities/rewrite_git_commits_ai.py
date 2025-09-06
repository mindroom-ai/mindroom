#!/usr/bin/env python3
"""Simple git commit message rewriter using agno with structured output.

Usage:
    python scripts/rewrite_git_commits_ai.py <commit_hash>    # Single commit
    python scripts/rewrite_git_commits_ai.py HEAD~5           # Relative reference
    python scripts/rewrite_git_commits_ai.py --all            # All commits
    python scripts/rewrite_git_commits_ai.py --range HEAD~10..HEAD  # Range
"""

import asyncio
import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import dotenv
from agno.agent import Agent
from agno.models.deepseek import DeepSeek
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMIT_DIR = REPO_ROOT / "commit_messages"
COMMIT_DIR.mkdir(exist_ok=True, parents=True)

dotenv.load_dotenv(REPO_ROOT / ".env")


class CommitRewrite(BaseModel):
    """Structured output for commit message rewriting."""

    commit_message: str = Field(description="The new or original commit message")
    reasoning: str = Field(description="Why this message was chosen (kept original or rewrote)")
    action: Literal["kept", "rewrote"] = Field(description="Either 'kept' or 'rewrote'")
    commit_hash: str = Field(default="", description="The commit hash")


async def rewrite_commit(commit_hash: str) -> CommitRewrite | None:
    """Rewrite a single commit message."""
    # Get the actual commit hash (in case user passed HEAD~5 etc)
    hash_result = subprocess.run(  # noqa: S602, ASYNC221
        f"git rev-parse {commit_hash}",
        check=False,
        shell=True,
        capture_output=True,
        text=True,
    )
    actual_hash = hash_result.stdout.strip()
    output_file = COMMIT_DIR / f"{actual_hash}.json"
    if output_file.exists():
        print(f"✅ Commit {actual_hash[:8]} already processed, skipping.")
        return None

    # Get the commit with stats first (summary of changes)
    stat_result = subprocess.run(  # noqa: S602, ASYNC221
        f"git show --stat {commit_hash}",
        check=False,
        shell=True,
        capture_output=True,
        text=True,
    )

    if stat_result.returncode != 0:
        print(f"Error: Could not get commit {commit_hash}")
        print(stat_result.stderr)
        sys.exit(1)

    # Get the full diff
    diff_result = subprocess.run(f"git show {commit_hash}", check=False, shell=True, capture_output=True, text=True)  # noqa: S602, ASYNC221

    # Truncate diff if it's too long (keep first 8000 chars to leave room for prompt)
    diff_content = diff_result.stdout
    max_diff_length = 8000
    if len(diff_content) > max_diff_length:
        # Find a good truncation point (end of a line)
        truncate_at = diff_content[:max_diff_length].rfind("\n")
        if truncate_at == -1:
            truncate_at = max_diff_length
        diff_content = diff_content[:truncate_at] + "\n\n... [DIFF TRUNCATED - COMMIT TOO LARGE] ..."

    # Combine stat summary and truncated diff
    commit_details = (
        f"=== COMMIT SUMMARY (--stat) ===\n{stat_result.stdout}\n\n=== DETAILED CHANGES ===\n{diff_content}"
    )

    # Create the agent with structured output
    agent = Agent(
        name="commit-rewriter",
        model=DeepSeek(id="deepseek-reasoner"),
        response_model=CommitRewrite,
    )

    # Create the prompt with all the commit details
    prompt = f"""Here is a git commit to analyze:

{commit_details}

Analyze this commit and decide if the message needs improvement based on this repository's style guide.

REPOSITORY COMMIT STYLE GUIDE:
- Always use conventional commit format: type(scope): description
- Types: feat, fix, docs, style, refactor, test, chore, perf, ci, build, revert
- Include scope in parentheses when there's a clear module/area affected
- Use lowercase after the colon (unless proper nouns like "Docker", "Matrix", "AI")
- Keep first line under 72 characters
- Use imperative mood ("Add" not "Added", "Fix" not "Fixed")
- Be specific and descriptive about what changed
- For bug fixes, briefly describe what was broken
- For features, describe what capability was added
- For refactors, explain what was restructured

GOOD EXAMPLES FROM THIS REPO:
- "feat: Add Direct Message (DM) support for private agent conversations (#127)"
- "fix(profile): Handle null user data in profile loading"
- "refactor: Extract team formation logic into private function"
- "test: Add comprehensive tests for extra_kwargs functionality"
- "docs: Add comprehensive deployment guide for instance manager"
- "fix: Use centralized credentials manager for Home Assistant integration"

BAD EXAMPLES TO REWRITE:
- Single words: "icons", "simpler", "yaml", "fixes"
- Typos: "udpate", "fxi", "chagne"
- Vague: "changes", "updates", "WIP", "stuff"
- Just filenames: "api.js", "config.yaml"
- No conventional format: "Fix router stt", "Remove lock files that are unused"

SPECIAL CASES TO KEEP:
- Merge commits: "Merge pull request 'branch' (#123) from branch into main"
- Reverts: Follow standard revert format
- Initial commits: Can be simple like "Initial commit"

DECISION:
If the original message already follows this style guide well, set action='kept'.
If it needs improvement to match this style, set action='rewrote' and create a message following the guide above."""

    # Get the response
    response = await agent.arun(prompt)
    result = response.content

    # Add the commit hash to the result
    result.commit_hash = actual_hash

    # Save as JSON
    output_file = COMMIT_DIR / f"{actual_hash}.json"
    data = result.model_dump()
    data["metrics"] = response.metrics
    with open(output_file, "w") as f:  # noqa: PTH123, ASYNC230
        json.dump(data, f, indent=2)

    # Print results
    print(f"✅ Commit: {actual_hash[:8]}")
    print(f"📝 Action: {result.action}")
    print(f"💭 Reasoning: {result.reasoning}")
    print(f"📄 JSON saved to: {output_file}")
    print(f"📊 Metrics: {response.metrics}")

    return result


def get_all_commits() -> list[str]:
    """Get all commit hashes in the repository."""
    result = subprocess.run("git rev-list HEAD", check=False, shell=True, capture_output=True, text=True)  # noqa: S602
    if result.returncode != 0:
        print(f"Error getting commits: {result.stderr}")
        sys.exit(1)
    return result.stdout.strip().split("\n")


def get_range_commits(range_spec: str) -> list[str]:
    """Get commits in a specific range."""
    result = subprocess.run(f"git rev-list {range_spec}", check=False, shell=True, capture_output=True, text=True)  # noqa: S602
    if result.returncode != 0:
        print(f"Error getting commits: {result.stderr}")
        sys.exit(1)
    return result.stdout.strip().split("\n")


# Global flag for graceful shutdown
shutdown_requested = False


def handle_shutdown(signum: int, frame: Any) -> None:  # noqa: ARG001, ANN401
    """Handle Ctrl+C gracefully."""
    global shutdown_requested
    if not shutdown_requested:
        print("\n\n⚠️  Shutdown requested. Finishing current tasks...")
        print("   (Press Ctrl+C again to force quit)")
        shutdown_requested = True
    else:
        print("\n❌ Force quitting...")
        sys.exit(1)


# Register signal handler
signal.signal(signal.SIGINT, handle_shutdown)


async def process_multiple_commits(commits: list[str], max_concurrent: int = 5) -> list:
    """Process multiple commits with concurrency control and graceful shutdown."""
    semaphore = asyncio.Semaphore(max_concurrent)
    results = []
    pending_tasks = []

    async def process_with_semaphore(commit: str) -> CommitRewrite | None:
        async with semaphore:
            try:
                return await rewrite_commit(commit)
            except Exception as e:
                print(f"❌ Error processing {commit[:8]}: {e}")
                return None

    # Process commits in batches to allow checking for shutdown
    for i, commit in enumerate(commits):
        if shutdown_requested:
            print(f"\n🛑 Stopping after {i} commits (shutdown requested)")
            break

        task = asyncio.create_task(process_with_semaphore(commit))
        pending_tasks.append(task)

        # Process in chunks to check for shutdown more frequently
        if len(pending_tasks) >= max_concurrent or i == len(commits) - 1:
            # Wait for current batch to complete
            batch_results = await asyncio.gather(*pending_tasks)
            results.extend([r for r in batch_results if r is not None])
            pending_tasks = []

            # Show progress
            if not shutdown_requested and i < len(commits) - 1:
                print(f"   Progress: {i + 1}/{len(commits)} commits processed", end="\r")

    # Final cleanup
    if pending_tasks:
        print("\n⏳ Waiting for remaining tasks to complete...")
        batch_results = await asyncio.gather(*pending_tasks)
        results.extend([r for r in batch_results if r is not None])

    return results


async def main() -> None:
    """Main entry point for the script."""
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        description="Rewrite git commit messages with AI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s HEAD                      # Single commit
  %(prog)s HEAD~5                    # Relative reference
  %(prog)s abc123def                 # Specific hash
  %(prog)s --all                     # All commits
  %(prog)s --range HEAD~10..HEAD     # Last 10 commits
  %(prog)s --all --concurrent 10     # All commits, 10 at a time""",
    )

    parser.add_argument("commit", nargs="?", help="Commit hash or reference")
    parser.add_argument("--all", action="store_true", help="Process all commits")
    parser.add_argument("--range", help="Process commits in range (e.g., HEAD~10..HEAD)")
    parser.add_argument("--concurrent", type=int, default=5, help="Max concurrent processes (default: 5)")

    args = parser.parse_args()

    # Determine what commits to process
    if args.all:
        print("🔍 Getting all commits...")
        commits = get_all_commits()
        print(f"📊 Found {len(commits)} commits to process")
        results = await process_multiple_commits(commits, args.concurrent)
    elif args.range:
        print(f"🔍 Getting commits in range: {args.range}")
        commits = get_range_commits(args.range)
        print(f"📊 Found {len(commits)} commits to process")
        results = await process_multiple_commits(commits, args.concurrent)
    elif args.commit:
        # Single commit
        result = await rewrite_commit(args.commit)
        results = [result] if result else []
    else:
        parser.print_help()
        sys.exit(1)

    # Summary
    if len(results) > 1:
        kept = sum(1 for r in results if r.action == "kept")
        rewrote = sum(1 for r in results if r.action == "rewrote")
        print(f"\n📊 Summary: {kept} kept, {rewrote} rewrote out of {len(results)} processed")
        print(f"📁 Results saved in: {COMMIT_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())
