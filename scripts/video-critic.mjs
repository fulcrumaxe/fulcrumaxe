import { readFileSync } from "node:fs";
const KEY = process.env.OPENROUTER_API_KEY;
if (!KEY) { console.error("no OPENROUTER_API_KEY"); process.exit(1); }
const img = readFileSync(process.argv[2]).toString("base64");
const refImg = readFileSync(process.argv[3]).toString("base64");
const brief = "BRIEF: A satellite character (see REFERENCE image) animates: it smiles and tilts/nods left-to-right introducing FOUR identical gold quarter notes in a horizontal row in front of it, then spins with a bounce. The 4 notes must stay identical and on a row, and each should pop ON the musical beat. Notes must be exactly 4, identical, no treble clef, no staff.";
const ask = "The STORYBOARD image is 6 frames (left-to-right, top-to-bottom) of a generated video, labeled with timestamps. The REFERENCE image is the character's required exact appearance. Evaluate the video and return ONLY JSON with fields: character_consistency_0_10 (does the satellite match the reference across frames), note_count (how many distinct notes in the row), notes_identical (bool), forbidden_symbols (array of any treble clef/staff/rests/extra notes seen), notes_in_row (bool), motion_matches_brief_0_10, issues (array of short strings), verdict ('pass'|'fail').";
const body = { model: "google/gemini-2.5-flash", messages: [{ role: "user", content: [
  { type: "text", text: brief + "\n\n" + ask },
  { type: "image_url", image_url: { url: `data:image/png;base64,${img}` } },
  { type: "text", text: "REFERENCE (required exact character appearance):" },
  { type: "image_url", image_url: { url: `data:image/png;base64,${refImg}` } },
] }] };
const r = await fetch("https://openrouter.ai/api/v1/chat/completions", { method: "POST", headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" }, body: JSON.stringify(body) });
const j = await r.json();
if (!r.ok) { console.error("ERR", r.status, JSON.stringify(j).slice(0,400)); process.exit(1); }
console.log(j.choices?.[0]?.message?.content ?? JSON.stringify(j).slice(0,400));
