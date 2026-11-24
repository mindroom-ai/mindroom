from mindroom.workflow import agent, foreach, phase

FINDINGS = {"type": "array", "items": {"type": "object", "required": ["file", "line", "claim"],
            "properties": {"file": {"type": "string"}, "line": {"type": "integer"}, "claim": {"type": "string"}}}}
VERDICT = {"type": "object", "required": ["real", "reason"],
           "properties": {"real": {"type": "boolean"}, "reason": {"type": "string"}}}

def main(args):
    phase("Audit")
    per_file = foreach(args["files"], lambda f: agent(f"Audit {f} for {args['issue']}.", schema=FINDINGS))
    findings = [x for group in per_file for x in group]
    phase("Verify")
    verdicts = foreach(findings, lambda x: agent(
        f"Try to disprove this finding. Read the code first.\n{x}", schema=VERDICT, model=args.get("checker_model")))
    real = [x | {"reason": v["reason"]} for x, v in zip(findings, verdicts) if v["real"]]
    return {"confirmed": real, "rejected": len(findings) - len(real)}
