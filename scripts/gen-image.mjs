// Generic image generator via OpenRouter Gemini image. Usage: node gen-image.mjs <out.png> "<prompt>"
import { writeFileSync } from "node:fs";
const KEY = process.env.OPENROUTER_API_KEY;
if (!KEY) { console.error("no OPENROUTER_API_KEY"); process.exit(1); }
const out = process.argv[2], prompt = process.argv[3];
const body = { model: "google/gemini-2.5-flash-image", modalities: ["image", "text"],
  messages: [{ role: "user", content: [{ type: "text", text: prompt }] }] };
const r = await fetch("https://openrouter.ai/api/v1/chat/completions", {
  method: "POST", headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" }, body: JSON.stringify(body) });
const j = await r.json();
if (!r.ok) { console.error("ERR", r.status, JSON.stringify(j).slice(0, 300)); process.exit(1); }
const uri = j?.choices?.[0]?.message?.images?.[0]?.image_url?.url;
if (!uri) { console.error("no image", JSON.stringify(j).slice(0, 300)); process.exit(1); }
writeFileSync(out, Buffer.from(uri.slice(uri.indexOf(";base64,") + 8), "base64"));
console.log("wrote", out);
