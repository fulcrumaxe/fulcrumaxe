// One-off: generate a single scene still via OpenRouter (production model),
// following the D#343 visual-only rule — the art leaves a CLEAN lower banner
// where code composites the 4 quarter notes (D#344) + pulse animation (D#349).
import { writeFileSync } from "node:fs";

const KEY = process.env.OPENROUTER_API_KEY;
if (!KEY) { console.error("no OPENROUTER_API_KEY"); process.exit(1); }

const MODEL = "google/gemini-2.5-flash-image";
const URL = "https://openrouter.ai/api/v1/chat/completions";
const OUT = process.argv[2] || "/tmp/scene.png";

const styleTag =
  "Bright, friendly 3D-storybook style for young children (ages 4-8); soft rounded shapes, " +
  "warm cheerful lighting, gentle saturated colors, clean uncluttered composition";

// Visual-only prompt: NO notes/text in the art. A clean wide ledge across the lower
// third is left empty on purpose — the deterministic note strip is composited there.
const imagePrompt =
  "A cheerful music classroom on a sunny morning. A friendly round little robot mascot with big " +
  "kind eyes sits at a child-sized white piano, one hand raised mid-gesture as if about to play. " +
  "Soft potted plants and a sunlit window in the background. Camera framed wide and slightly low, " +
  "leaving the entire lower third of the frame as a clean, empty, softly-lit wooden ledge surface " +
  "with nothing on it. Plain, unmarked surfaces everywhere — no notation, no markings. " +
  "Warm, inviting, calm mood. 16:9 widescreen composition.";

const body = {
  model: MODEL,
  modalities: ["image", "text"],
  messages: [{ role: "user", content: [{ type: "text", text: `${styleTag}. ${imagePrompt}` }] }],
};

const res = await fetch(URL, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${KEY}` },
  body: JSON.stringify(body),
});
if (!res.ok) { console.error("OpenRouter error", res.status, await res.text()); process.exit(1); }
const json = await res.json();
const dataUri = json?.choices?.[0]?.message?.images?.[0]?.image_url?.url;
if (!dataUri) { console.error("no image in response", JSON.stringify(json).slice(0, 400)); process.exit(1); }
const b64 = dataUri.slice(dataUri.indexOf(";base64,") + 8);
const bytes = Buffer.from(b64, "base64");
writeFileSync(OUT, bytes);
console.log(`wrote ${OUT} ${bytes.length} bytes`);
