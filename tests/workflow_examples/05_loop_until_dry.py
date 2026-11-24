from mindroom.workflow import agent, foreach, log, phase

ISSUES = {"type": "array", "items": {"type": "object", "required": ["id", "text"],
          "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}}

def main(args):
    found = {}
    for round in range(1, args.get("max_rounds", 8) + 1):
        phase(f"Round {round}")
        known = "\n".join(f"- {text}" for text in found.values()) or "(none yet)"
        batches = foreach(args["areas"], lambda area: agent(
            f"Find bugs in {area}. Skip these known ones:\n{known}\nGive each bug a short stable id.", schema=ISSUES))
        new = {i["id"]: i["text"] for batch in batches for i in batch if i["id"] not in found}
        log(f"Round {round}: {len(new)} new, {len(found)} known")
        if not new:
            break
        found |= new
    return [{"id": key, "text": text} for key, text in found.items()]
