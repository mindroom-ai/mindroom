from mindroom.workflow import spawn

def main(args):
    pro = spawn("pro", system="Argue for the proposal. Concede what is right.")
    con = spawn("con", system="Argue against the proposal. Concede what is right.")
    turns = [args["proposal"]]
    for _ in range(3):
        turns.append(pro.ask("Respond to this:\n" + turns[-1]))
        turns.append(con.ask("Respond to this:\n" + turns[-1]))
    return spawn("judge").ask("Decide, and say why.\n\n" + "\n\n---\n\n".join(turns))
