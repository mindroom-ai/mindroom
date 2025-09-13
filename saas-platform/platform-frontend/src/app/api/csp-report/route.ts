export async function POST(req: Request): Promise<Response> {
  try {
    // Best-effort drain the body to avoid hanging connections.
    await req.json().catch(() => undefined)
  } catch {
    // ignore parse errors
  }
  // Return 204 No Content without logging sensitive report data.
  return new Response(null, { status: 204 })
}
