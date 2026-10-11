// Packaged trusted init-page hook. Never load model-authored code in Node.
"use strict";
const contexts = new WeakMap();
const endpoint = process.env.MINDROOM_BROWSER_VERIFY_ENDPOINT;
const token = process.env.MINDROOM_BROWSER_VERIFY_TOKEN;

exports.default = async ({ page }) => {
  const context = page.context();
  let installation = contexts.get(context);
  if (!installation) {
    installation = (async () => {
      if (!endpoint || !token) throw new Error("Browser request verifier unavailable");
      await context.route("**/*", async (route) => {
        let allowed = false;
        try {
          const response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
            body: JSON.stringify({ url: route.request().url() }),
            signal: AbortSignal.timeout(10000),
            redirect: "error",
          });
          const body = await response.text();
          if (response.ok && body.length <= 128) {
            const value = JSON.parse(body);
            allowed = value !== null && typeof value === "object" &&
              Object.keys(value).length === 1 && value.allowed === true;
          }
        } catch (_) { /* Deny without logging URLs or credentials. */ }
        if (allowed) await route.continue();
        else await route.abort("blockedbyclient");
      });
    })();
    contexts.set(context, installation);
  }
  await installation;
};
