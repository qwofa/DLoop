// Usage: node tests/check_ui_annotation_render.cjs PLAN RAW PNG_OUTPUT X Y WIDTH HEIGHT
// Requires playwright, sharp, marked and DLOOP_TEST_CHROME (a Chrome executable).
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const sharp = require('sharp');
const { marked } = require('marked');

async function main() {
  const [planArg, rawArg, output, ...coordinates] = process.argv.slice(2);
  if (!output || coordinates.length !== 4 || !process.env.DLOOP_TEST_CHROME) {
    throw new Error('Provide PLAN RAW PNG_OUTPUT X Y WIDTH HEIGHT and DLOOP_TEST_CHROME');
  }
  const plan = path.resolve(planArg);
  const raw = path.resolve(rawArg);
  const root = path.dirname(path.dirname(plan));
  const region = Object.fromEntries(['left', 'top', 'width', 'height'].map((key, i) => [key, Number(coordinates[i])]));
  const markdown = fs.readFileSync(plan, 'utf8').replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, '');
  const html = '<style>body{background:#181818;color:#eee;font:20px sans-serif;margin:40px}img{max-width:100%}table{border-collapse:collapse}td,th{border:1px solid #555;padding:12px}a{color:#77aaff}</style>' + marked.parse(markdown);
  const server = http.createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
    if (pathname === '/04-plan/preview.html') {
      response.setHeader('content-type', 'text/html; charset=utf-8');
      response.end(html);
      return;
    }
    const file = path.resolve(root, '.' + pathname);
    const relative = path.relative(root, file);
    if (relative.startsWith('..') || path.isAbsolute(relative) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      response.writeHead(404).end();
      return;
    }
    response.setHeader('content-type', file.endsWith('.svg') ? 'image/svg+xml' : 'image/png');
    response.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({executablePath:process.env.DLOOP_TEST_CHROME, headless:true});
    const page = await browser.newPage({viewport:{width:2460,height:1200}, deviceScaleFactor:1});
    await page.goto(`http://127.0.0.1:${server.address().port}/04-plan/preview.html`);
    await page.locator('img').evaluateAll(images => Promise.all(images.map(image => image.decode())));
    const imageSources = await page.locator('img').evaluateAll(images => images.map(image => image.getAttribute('src')));
    const rawVisible = imageSources.some(source => source.endsWith(path.basename(raw)));
    const pngLinks = await page.locator('a[href$=".png"]').count();
    const annotation = page.locator('img[src$="-annotated.svg"]');
    if (await annotation.count() !== 1) throw new Error('Select a plan with exactly one annotated screenshot');
    const rendered = await annotation.screenshot();
    const expected = await sharp(raw).extract(region).removeAlpha().raw().toBuffer();
    const actual = await sharp(rendered).extract(region).removeAlpha().raw().toBuffer();
    let matching = 0;
    for (let i=0; i<expected.length; i+=3) {
      if ([0,1,2].every(channel => Math.abs(actual[i+channel]-expected[i+channel]) < 4)) matching++;
    }
    const fraction = matching/(region.width*region.height);
    await page.screenshot({path:output, fullPage:true});
    const pass = rawVisible && pngLinks===0 && fraction>0.95;
    console.log(JSON.stringify({rawVisible,pngLinks,matchingScreenshotPixels:fraction,pass}));
    if (!pass) process.exitCode=1;
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
