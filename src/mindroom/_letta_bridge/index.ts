import { LettaAgentClient, type LettaCodeSession } from "@letta-ai/letta-agent-sdk";

type TurnRequest = {
  agentId: string;
  conversationId?: string;
  prompt: string;
  cwd?: string;
};

const request = JSON.parse(await readStdin()) as TurnRequest;
const client = new LettaAgentClient({
  backend: "cloud",
  ...(process.env.LETTA_BASE_URL ? { apiBaseUrl: process.env.LETTA_BASE_URL } : {}),
});
const options = {
  permissionMode: "unrestricted" as const,
  ...(request.cwd ? { cwd: request.cwd } : {}),
};
const session = request.conversationId
  ? client.resumeSession(request.conversationId, options)
  : client.createSession(request.agentId, options);

let aborting = false;
const abort = async () => {
  if (aborting) return;
  aborting = true;
  await session.abort();
};
process.once("SIGTERM", () => void abort());
process.once("SIGINT", () => void abort());

try {
  const ready = await session.ready();
  process.stdout.write(
    `${JSON.stringify({ type: "session", conversationId: ready.conversationId })}\n`,
  );
  await session.send(request.prompt);
  for await (const message of session.stream()) {
    process.stdout.write(`${JSON.stringify(message)}\n`);
  }
} finally {
  await dispose(session);
}

async function readStdin(): Promise<string> {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  return input;
}

async function dispose(sessionToDispose: LettaCodeSession): Promise<void> {
  await sessionToDispose[Symbol.asyncDispose]();
}
