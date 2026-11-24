from mindroom.workflow import ask_user, phase, shell, spawn

def main(args):
    wt = args["worktree"]
    dev = spawn("dev")
    reviewer = spawn("reviewer", model=args.get("review_model"),
                     system="You review diffs. Reply APPROVE or list blocking problems.")
    phase("Implement")
    dev.ask(f"Implement this in {wt} with tests, and commit:\n{args['task']}")
    round, limit = 0, 5
    while True:
        round += 1
        phase(f"Round {round}")
        tests = shell(f"cd {wt} && {args['test_command']}")
        review = reviewer.ask(f"Review `git diff main` in {wt}.")
        if tests.ok and review.strip().startswith("APPROVE"):
            return f"Done in {round} round(s).\n\n{review}"
        if round == limit:
            if ask_user(f"{round} rounds did not converge. Keep going?", ["Stop here", "Five more rounds"]) == "Stop here":
                return f"Stopped after {round} rounds.\n\nTests:\n{tests.output}\n\nReview:\n{review}"
            limit += 5
        dev.ask(f"Fix and commit.\n\nTest output:\n{tests.output}\n\nReview:\n{review}")
