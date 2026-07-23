// Vendored ai-gateway client for application runtime code (not CI).
//
// Copy this single file into your app. It fetches the shared provider/model
// config from ai-gateway's models.json on each call, so swapping a model or
// adding a provider (e.g. a paid one like DeepSeek) is a one-line edit in
// ai-gateway -- this file and your app code never change. If the fetch fails
// (GitHub outage), it falls back to OpenRouter's free auto-router so the app
// doesn't hard-fail on a transient network issue.
//
// Usage:
//   const { complete } = require("./gateway-client");
//   const text = await complete("Summarize this weather forecast: ...");
//   const text = await complete("...", { cascade: "deepseek_cheap" }); // paid, needs DEEPSEEK_API_KEY
//
// Env: one API key per provider you use (OPENROUTER_API_KEY, DEEPSEEK_API_KEY,
//      GEMINI_API_KEY, GROQ_API_KEY). Only providers with a key set are tried.

const CONFIG_URL =
  "https://raw.githubusercontent.com/albertolive/ai-gateway/main/models.json";

const FALLBACK_PROVIDERS = {
  openrouter: { url: "https://openrouter.ai/api/v1", key_env: "OPENROUTER_API_KEY" },
};
const FALLBACK_ENTRIES = [{ provider: "openrouter", model: "openrouter/free" }];

async function loadConfig(timeoutMs = 10000) {
  try {
    const res = await fetch(CONFIG_URL, { signal: AbortSignal.timeout(timeoutMs) });
    return await res.json();
  } catch {
    return { providers: FALLBACK_PROVIDERS, cascades: {} };
  }
}

async function complete(prompt, { cascade = "general", system, temperature = 0.1 } = {}) {
  const config = await loadConfig();
  const providers = Object.keys(config.providers || {}).length
    ? config.providers
    : FALLBACK_PROVIDERS;
  const entries = (config.cascades && config.cascades[cascade]) || FALLBACK_ENTRIES;

  const errors = [];
  for (const entry of entries) {
    const provider = providers[entry.provider];
    if (!provider) {
      errors.push(`${entry.provider}/${entry.model}: unknown provider '${entry.provider}'`);
      continue;
    }
    const apiKey = process.env[provider.key_env];
    if (!apiKey) {
      errors.push(`${entry.provider}/${entry.model}: ${provider.key_env} not set`);
      continue;
    }

    const messages = [
      ...(system ? [{ role: "system", content: system }] : []),
      { role: "user", content: prompt },
    ];

    try {
      const res = await fetch(`${provider.url.replace(/\/$/, "")}/chat/completions`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ model: entry.model, messages, temperature }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      const body = await res.json();
      return body.choices[0].message.content;
    } catch (e) {
      errors.push(`${entry.provider}/${entry.model}: ${e}`);
    }
  }

  throw new Error(
    `All providers in cascade '${cascade}' failed or were unconfigured:\n` +
      errors.map((e) => `  - ${e}`).join("\n")
  );
}

module.exports = { complete };
