// Meeting summaries for the news reader site.
// The page sends a meeting transcript with the signed-in user's Firebase ID token; this Worker checks that the
// token is real and belongs to an allowed account, then asks Claude for a summary. The Claude API key stays here
// (a Worker secret) and never reaches the browser.
//
// Settings (wrangler.toml vars): FIREBASE_PROJECT_ID, ALLOWED_ORIGIN.
// Secrets: ANTHROPIC_API_KEY, ALLOWED_UIDS (comma-separated Firebase user IDs).

import Anthropic from "@anthropic-ai/sdk";
import { createRemoteJWKSet, jwtVerify } from "jose";

const MODEL = "claude-haiku-5-5";
const MAX_TRANSCRIPT_CHARS = 400_000;
const GOOGLE_KEYS = createRemoteJWKSet(
  new URL("https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"),
);

const SUMMARY_SCHEMA = {
  type: "object",
  properties: {
    overview: { type: "string", description: "会議の要点を3文以内で" },
    points: { type: "array", items: { type: "string" }, description: "話し合われた主な内容" },
    decisions: { type: "array", items: { type: "string" }, description: "決まったこと" },
    actions: {
      type: "array",
      items: {
        type: "object",
        properties: {
          task: { type: "string" },
          owner: { type: "string", description: "担当者。分からなければ空文字" },
          due: { type: "string", description: "期限。分からなければ空文字" },
        },
        required: ["task", "owner", "due"],
        additionalProperties: false,
      },
    },
    open_questions: { type: "array", items: { type: "string" }, description: "持ち越し・未決のこと" },
  },
  required: ["overview", "points", "decisions", "actions", "open_questions"],
  additionalProperties: false,
};

const PROMPT = `次は会議の文字起こしです。音声認識で自動作成したもので、誤字や聞き間違い、話者の区別がないことがあります。
内容をもとに、日本語で会議の要約を作ってください。
- 文字起こしに書かれていないことは推測で補わない
- 担当者や期限は、発言から読み取れる場合だけ書く
- 聞き間違いと思われる語は、文脈から明らかな場合だけ直す
- 各項目は短く、具体的に`;

function cors(env, extra = {}) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
    ...extra,
  };
}

function reply(env, status, body) {
  return new Response(JSON.stringify(body), { status, headers: cors(env, { "Content-Type": "application/json" }) });
}

async function signedInUid(request, env) {
  const token = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, GOOGLE_KEYS, {
      issuer: `https://securetoken.google.com/${env.FIREBASE_PROJECT_ID}`,
      audience: env.FIREBASE_PROJECT_ID,
    });
    return payload.sub || null;
  } catch {
    return null;
  }
}

async function summarize(env, title, transcript) {
  const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
  const response = await client.messages.create({
    model: MODEL,
    max_tokens: 16000,
    output_config: { effort: "medium", format: { type: "json_schema", schema: SUMMARY_SCHEMA } },
    messages: [{
      role: "user",
      content: `${PROMPT}\n\n会議名：${title || "（なし）"}\n\n<transcript>\n${transcript}\n</transcript>`,
    }],
  });
  if (response.stop_reason !== "end_turn") throw new Error(`stopped: ${response.stop_reason}`);
  const text = response.content.find((b) => b.type === "text")?.text;
  return JSON.parse(text);
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(env) });
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/summarize") return reply(env, 404, { error: "not_found" });
    if (request.headers.get("Origin") !== env.ALLOWED_ORIGIN) return reply(env, 403, { error: "origin" });

    const uid = await signedInUid(request, env);
    if (!uid) return reply(env, 401, { error: "login" });
    const allowed = (env.ALLOWED_UIDS || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (!allowed.includes(uid)) return reply(env, 403, { error: "not_allowed", uid });

    let body;
    try { body = await request.json(); } catch { return reply(env, 400, { error: "bad_json" }); }
    const transcript = String(body.transcript || "").trim();
    if (!transcript) return reply(env, 400, { error: "empty" });
    if (transcript.length > MAX_TRANSCRIPT_CHARS) return reply(env, 413, { error: "too_long" });

    try {
      const summary = await summarize(env, String(body.title || "").slice(0, 200), transcript);
      return reply(env, 200, { summary, model: MODEL });
    } catch (error) {
      if (error instanceof Anthropic.AuthenticationError) return reply(env, 502, { error: "api_key" });
      if (error instanceof Anthropic.RateLimitError) return reply(env, 429, { error: "rate_limited" });
      if (error instanceof Anthropic.APIError) return reply(env, 502, { error: "api", status: error.status });
      return reply(env, 502, { error: "failed" });
    }
  },
};
