from mindroom.workflow import agent, parallel, phase, state

def backend(feature):
    return agent(f"Build the API for: {feature}\nBefore you write code, agree on the JSON contract with the agent named "
                 "'frontend': send_peer_message('frontend', ...), then read_peer_messages(wait_seconds=600) for its reply. "
                 "Save the agreed contract with set_shared_state('contract', ...).", name="backend")

def frontend(feature):
    return agent(f"Build the UI for: {feature}\nThe agent named 'backend' will propose a JSON contract: "
                 "read_peer_messages(wait_seconds=600), push back where it does not fit the UI, then build against it.",
                 name="frontend")

def main(args):
    phase("Build")
    api, ui = parallel([lambda: backend(args["feature"]), lambda: frontend(args["feature"])])
    phase("Integrate")
    return agent(f"Report where either side breaks this contract.\n\nContract: {state.get('contract')}"
                 f"\n\nBackend:\n{api}\n\nFrontend:\n{ui}")
