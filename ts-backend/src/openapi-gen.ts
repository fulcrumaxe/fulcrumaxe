/**
 * openapi-gen.ts — CLI script to regenerate the committed openapi.json snapshot.
 *
 * Usage: bun run openapi:gen
 *   Writes ts-backend/openapi.json relative to this file's location.
 *
 * The committed snapshot is a drift guard: if the generator output changes,
 * the test suite catches it (see tests/openapi.test.ts).
 */

import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { buildOpenApiDocument } from "./openapi.js";

const doc = buildOpenApiDocument();
const outPath = join(import.meta.dir, "..", "openapi.json");
writeFileSync(outPath, JSON.stringify(doc, null, 2) + "\n", "utf-8");
console.log(`[openapi:gen] Wrote ${outPath}`);
