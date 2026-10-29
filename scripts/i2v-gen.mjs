// Image-to-video via OpenRouter /api/v1/videos (Seedance 2.0 — strongest char consistency).
// Usage: node i2v-gen.mjs <inputPng> <outMp4> [durationSec] [model]
import { readFileSync, writeFileSync } from "node:fs";

const KEY = process.env.OPENROUTER_API_KEY;
if (!KEY) { console.error("no OPENROUTER_API_KEY"); process.exit(1); }
const IN = process.argv[2];
const OUT = process.argv[3];
const DUR = parseInt(process.argv[4] || "6", 10);
const MODEL = process.argv[5] || "bytedance/seedance-2.0";
const BASE = "https://openrouter.ai/api/v1/videos";
const H = { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" };

const img = readFileSync(IN);
const dataUri = `data:image/png;base64,${img.toString("base64")}`;

// ANIMATION ONLY — describe just the satellite's performance. NO mention of notes (even
// negatively), since naming a concept primes the model to render it. The 4 quarter notes
// are added entirely by code in a separate post-process compositing pass.
const prompt =
  "Close-up three-quarter view of the satellite character, floating gently in space. " +
  "It keeps its EXACT appearance from the reference image — same face, same gold body, same blue " +
  "solar panels — completely unchanged throughout. " +
  "The satellite smiles warmly and gently tilts and nods from left to right, pausing briefly at four " +
  "evenly-spaced moments as it looks across the space in front of it. " +
  "Then it does a small cheerful spin in place with a happy little bounce, and settles facing forward, " +
  "smiling at the viewer. " +
  "Bright, colorful, simple 3D cartoon animation with gentle sparkles and a joyful, magical atmosphere.";

const body = {
  model: MODEL,
  prompt,
  frame_images: [{ type: "image_url", image_url: { url: dataUri }, frame_type: "first_frame" }],
  resolution: "1080p",
  aspect_ratio: "4:3",
  duration: DUR,
};

console.log(`[i2v] submitting ${MODEL} dur=${DUR}s 1080p 4:3 ...`);
const sub = await fetch(BASE, { method: "POST", headers: H, body: JSON.stringify(body) });
const subText = await sub.text();
if (!sub.ok) { console.error("submit failed", sub.status, subText); process.exit(1); }
let job = JSON.parse(subText);
const id = job.id;
console.log(`[i2v] job id=${id} status=${job.status}`);

const t0 = Date.now();
const MAX_MS = 12 * 60 * 1000;
while (true) {
  await new Promise((r) => setTimeout(r, 8000));
  const poll = await fetch(`${BASE}/${id}`, { headers: H });
  const pj = await poll.json().catch(() => ({}));
  const st = pj.status || pj.state;
  const el = Math.round((Date.now() - t0) / 1000);
  console.log(`[i2v] +${el}s status=${st}`);
  if (st === "completed" || st === "succeeded") { job = pj; break; }
  if (st === "failed" || st === "error" || pj.error) { console.error("gen failed", JSON.stringify(pj).slice(0, 600)); process.exit(1); }
  if (Date.now() - t0 > MAX_MS) { console.error("timeout"); process.exit(1); }
}

const url = (job.unsigned_urls && job.unsigned_urls[0]) || `${BASE}/${id}/content?index=0`;
console.log(`[i2v] downloading ${url}`);
const vid = await fetch(url, { headers: H });
if (!vid.ok) { console.error("download failed", vid.status, await vid.text()); process.exit(1); }
const buf = Buffer.from(await vid.arrayBuffer());
writeFileSync(OUT, buf);
console.log(`[i2v] wrote ${OUT} ${buf.length} bytes`);
