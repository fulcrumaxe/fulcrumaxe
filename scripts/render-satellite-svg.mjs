// Code-drawn (SVG) rendition of the reference satellite character → PNG via resvg.
// 100% code-owned: identical every frame. Run: node scripts/render-satellite-svg.mjs out.png
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { initWasm, Resvg } from "@resvg/resvg-wasm";

// Resolve the wasm binary via Node's own module resolution (works from this repo's
// node_modules regardless of hoisting), with an env-var escape hatch.
const require = createRequire(import.meta.url);
const WASM = process.env.RESVG_WASM_PATH || require.resolve("@resvg/resvg-wasm/index_bg.wasm");
await initWasm(readFileSync(WASM));

const W = 1200, H = 900, cx = 560, cy = 470;

// a few deterministic background stars
let stars = "";
const sx = [80,180,300,420,520,700,900,1050,1130,140,360,640,820,980,1100,60,260,480,760,1000];
const sy = [80,160,60,200,120,90,150,70,180,300,360,260,330,300,250,560,640,720,600,680];
for (let i=0;i<sx.length;i++){ const r = (i%3)+1.5; stars += `<circle cx="${sx[i]}" cy="${sy[i]}" r="${r}" fill="#cdd6ff" opacity="${0.5+0.5*((i%2))}"/>`; }

// one solar panel (3x2 blue cells, gold frame), drawn at origin then placed/rotated
function panel(x,y,rot){
  const pw=230, ph=150, cellsX=3, cellsY=2, gap=8, fr=14;
  let cells="";
  const iw=(pw-fr*2-gap*(cellsX-1))/cellsX, ih=(ph-fr*2-gap*(cellsY-1))/cellsY;
  for(let r=0;r<cellsY;r++)for(let c=0;c<cellsX;c++){
    const px=fr+c*(iw+gap), py=fr+r*(ih+gap);
    cells+=`<rect x="${px}" y="${py}" width="${iw}" height="${ih}" rx="6" fill="url(#cell)" stroke="#0a1f6e" stroke-width="2"/>`;
  }
  return `<g transform="translate(${x},${y}) rotate(${rot})">
    <rect x="0" y="0" width="${pw}" height="${ph}" rx="18" fill="url(#frame)" stroke="#b8860b" stroke-width="3"/>
    ${cells}</g>`;
}

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
<defs>
  <radialGradient id="space" cx="45%" cy="40%" r="80%">
    <stop offset="0%" stop-color="#2a2b66"/><stop offset="55%" stop-color="#161a44"/><stop offset="100%" stop-color="#0a0c24"/>
  </radialGradient>
  <radialGradient id="face" cx="38%" cy="32%" r="75%">
    <stop offset="0%" stop-color="#7fe6f5"/><stop offset="45%" stop-color="#36b9d9"/><stop offset="100%" stop-color="#1f87b0"/>
  </radialGradient>
  <linearGradient id="body" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="#f4c145"/><stop offset="45%" stop-color="#d99a23"/><stop offset="100%" stop-color="#9c6a12"/>
  </linearGradient>
  <linearGradient id="frame" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="#f0c24a"/><stop offset="100%" stop-color="#b07d18"/>
  </linearGradient>
  <radialGradient id="cell" cx="35%" cy="30%" r="80%">
    <stop offset="0%" stop-color="#3a6bff"/><stop offset="100%" stop-color="#13267e"/>
  </radialGradient>
  <radialGradient id="ant" cx="40%" cy="30%" r="80%">
    <stop offset="0%" stop-color="#f4cf6a"/><stop offset="100%" stop-color="#a9760f"/>
  </radialGradient>
</defs>

<rect width="${W}" height="${H}" fill="url(#space)"/>
${stars}

<!-- solar panels behind body -->
${panel(120, 360, -18)}
${panel(860, 250, 12)}
<!-- struts -->
<rect x="320" y="455" width="120" height="26" rx="13" fill="url(#body)"/>
<rect x="760" y="420" width="120" height="26" rx="13" fill="url(#body)"/>

<!-- gold body capsule -->
<ellipse cx="${cx+120}" cy="${cy}" rx="250" ry="190" fill="url(#body)" stroke="#8a5e10" stroke-width="4"/>
<ellipse cx="${cx+250}" cy="${cy}" rx="70" ry="150" fill="#c98e20" opacity="0.5"/>
<!-- thrusters -->
<rect x="${cx+300}" y="${cy-30}" width="90" height="60" rx="14" fill="url(#body)" stroke="#8a5e10" stroke-width="3"/>
<rect x="${cx+120}" y="${cy+170}" width="60" height="90" rx="14" fill="url(#body)" stroke="#8a5e10" stroke-width="3"/>

<!-- antenna -->
<g transform="translate(${cx-120},${cy-230}) rotate(-18)">
  <rect x="-12" y="40" width="24" height="120" rx="10" fill="url(#ant)"/>
  <ellipse cx="0" cy="36" rx="60" ry="34" fill="url(#ant)" stroke="#8a5e10" stroke-width="3"/>
  <circle cx="0" cy="30" r="12" fill="#ffd86b"/>
</g>
<g transform="translate(${cx+40},${cy-220})">
  <rect x="-7" y="0" width="14" height="80" rx="7" fill="url(#ant)"/>
  <circle cx="0" cy="-6" r="14" fill="#ffd86b" stroke="#a9760f" stroke-width="2"/>
</g>

<!-- blue face -->
<circle cx="${cx}" cy="${cy}" r="205" fill="url(#face)" stroke="#16678a" stroke-width="5"/>
<ellipse cx="${cx-70}" cy="${cy-95}" rx="80" ry="50" fill="#ffffff" opacity="0.18"/>
<!-- cheeks -->
<ellipse cx="${cx-110}" cy="${cy+55}" rx="40" ry="26" fill="#ff7aa8" opacity="0.55"/>
<ellipse cx="${cx+110}" cy="${cy+55}" rx="40" ry="26" fill="#ff7aa8" opacity="0.55"/>
<!-- eyes -->
<g>
  <ellipse cx="${cx-78}" cy="${cy-30}" rx="46" ry="58" fill="#ffffff"/>
  <ellipse cx="${cx+78}" cy="${cy-30}" rx="46" ry="58" fill="#ffffff"/>
  <circle cx="${cx-70}" cy="${cy-18}" r="30" fill="#15314f"/>
  <circle cx="${cx+86}" cy="${cy-18}" r="30" fill="#15314f"/>
  <circle cx="${cx-80}" cy="${cy-28}" r="10" fill="#ffffff"/>
  <circle cx="${cx+76}" cy="${cy-28}" r="10" fill="#ffffff"/>
</g>
<!-- smile -->
<path d="M ${cx-95} ${cy+70} Q ${cx} ${cy+185} ${cx+95} ${cy+70} Q ${cx} ${cy+120} ${cx-95} ${cy+70} Z" fill="#5a1722"/>
<path d="M ${cx-60} ${cy+110} Q ${cx} ${cy+165} ${cx+60} ${cy+110} Q ${cx} ${cy+135} ${cx-60} ${cy+110} Z" fill="#ff5d7d"/>
</svg>`;

const png = new Resvg(svg, { fitTo: { mode: "width", value: W } }).render().asPng();
writeFileSync(process.argv[2] || "/tmp/satellite-svg.png", Buffer.from(png));
console.log("wrote", process.argv[2], png.length, "bytes");
