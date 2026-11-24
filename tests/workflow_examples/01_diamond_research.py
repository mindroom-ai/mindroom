from mindroom.workflow import PartialFailure, agent, foreach, phase

def main(args):
    phase("Plan")
    plan = agent(f"Split this question into 4-8 independent research angles: {args['question']}",
                 schema={"type": "array", "items": {"type": "string"}})
    phase("Research")
    try:
        notes, missing = foreach(plan, lambda angle: agent(f"Research: {angle}\nReturn findings with sources.")), []
    except PartialFailure as e:
        notes, missing = e.ok, [plan[i] for i in e.errors]
    phase("Synthesize")
    return agent("Answer the question from these notes. Mark anything only one note supports.\n\n"
                 f"Question: {args['question']}\nAngles with no research (say so): {missing}\n\n" + "\n\n---\n\n".join(notes))
