// Builds docs/DESIGN.pdf from docs/DESIGN.md: renders each ```mermaid block to PNG with mermaid-cli,
// converts the markdown to HTML and prints it with a "Page X of Y" footer.
// Usage: node docs/build-design.mjs <tools-dir>   (a folder holding node_modules with @mermaid-js/mermaid-cli,
// marked and puppeteer-core), env CHROME=/path/to/chrome.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";

const tools = resolve(process.argv[2] ?? ".");
const require = createRequire(join(tools, "package.json"));
const { marked } = require("marked");
const puppeteer = require("puppeteer-core");
const chrome = process.env.CHROME ?? "/usr/bin/google-chrome";
const root = resolve(new URL("..", import.meta.url).pathname);
const out = join(tools, "build");
mkdirSync(out, { recursive: true });

// width of each figure on the page, in mm (chosen so its text stays at 8pt or more)
const WIDTH = { legend: 160, fig1a: 186, fig1b: 180, fig2: 178, fig3a: 186, fig3b: 150 };
const mmConfig = join(tools, "mm.json");
const pp = join(tools, "pp.json");

let md = readFileSync(join(root, "docs/DESIGN.md"), "utf8");
const blocks = [...md.matchAll(/```mermaid\n([\s\S]*?)```/g)];
for (const m of blocks) {
  const id = /%% id=(\w+)/.exec(m[1])[1];
  const src = join(out, `${id}.mmd`);
  writeFileSync(src, m[1]);
  execFileSync(join(tools, "node_modules/.bin/mmdc"),
    ["-p", pp, "-c", mmConfig, "-i", src, "-o", join(out, `${id}.png`), "-b", "white", "-s", "3"], { stdio: "inherit" });
  md = md.replace(m[0], `<p class="fig"><img src="${id}.png" style="width:${WIDTH[id]}mm"></p>`);
}
const css = `
@page { size: A4; margin: 9mm 12mm 13mm 12mm; }
body { font: 10pt/1.2 Helvetica, Arial, sans-serif; color: #111827; margin: 0; }
h1 { font-size: 18pt; margin: 0 0 3mm; }
h2 { font-size: 12pt; margin: 3mm 0 1mm; border-bottom: 0.4pt solid #cbd5e1; padding-bottom: 0.6mm; break-after: avoid; }
p { margin: 0 0 1.2mm; text-align: left; }
.fig { text-align: center; margin: 1.2mm 0 1.2mm; break-inside: avoid; }
.fig img { max-width: 100%; }
table { border-collapse: collapse; width: 100%; margin: 1mm 0 2mm; font-size: 10pt; }
tr { break-inside: avoid; }
th, td { border: 0.4pt solid #cbd5e1; padding: 0.4mm 1.4mm; vertical-align: top; text-align: left; }
th { background: #f1f5f9; }
ul { margin: 0 0 1.6mm; padding-left: 5mm; } li { margin: 0 0 0.8mm; }
code { font: 9.5pt Menlo, Consolas, monospace; background: #f1f5f9; padding: 0 0.6mm; }
`;
const html = `<!doctype html><html><head><meta charset="utf-8"><style>${css}</style></head><body>${marked.parse(md)}</body></html>`;
writeFileSync(join(out, "design.html"), html);
const browser = await puppeteer.launch({ executablePath: chrome, args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.goto("file://" + join(out, "design.html"), { waitUntil: "load" });
await page.pdf({
  path: join(root, "docs/DESIGN.pdf"), format: "A4", printBackground: true, displayHeaderFooter: true,
  headerTemplate: "<span></span>",
  footerTemplate: `<div style="font: 8pt Helvetica, Arial, sans-serif; color:#475569; width:100%; text-align:center;">Agentic Revenue System · Design · Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>`,
  margin: { top: "9mm", bottom: "13mm", left: "12mm", right: "12mm" },
});
await browser.close();
console.log("wrote docs/DESIGN.pdf");
