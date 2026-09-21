"""Process-wide deny guard for offline commands, including transitive imports."""

import sys


def deny_network() -> None:
    """Irreversibly deny sockets and subprocess escape in this CLI process."""

    def audit(event: str, _args: tuple) -> None:
        if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")) or event == "os.system":
            msg = "network and subprocess access denied in offline replay"
            raise PermissionError(msg)

    sys.addaudithook(audit)
