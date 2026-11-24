from mindroom.workflow import agent, foreach, phase

SCORE = {"type": "object", "required": ["score", "why"],
         "properties": {"score": {"type": "number"}, "why": {"type": "string"}}}

def main(args):
    phase("Draft")
    plans = foreach(args["models"], lambda m: agent(f"Write a plan for: {args['task']}", model=m, name=f"planner-{m}"))
    phase("Judge")
    scores = foreach(plans, lambda p: agent("Score this plan 1-10. Be harsh.\n\n" + p, schema=SCORE))
    best = max(range(len(plans)), key=lambda i: scores[i]["score"])
    phase("Merge")
    others = "\n\n".join(p for i, p in enumerate(plans) if i != best)
    return agent(f"Improve the winning plan with the best ideas of the others.\n\nWinner:\n{plans[best]}\n\nOthers:\n{others}")
