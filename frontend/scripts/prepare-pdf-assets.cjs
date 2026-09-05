// Keep the worker, fonts and CMaps at the same version as the bundled client.
const fs = require('node:fs');
const path = require('node:path');
const source = path.dirname(require.resolve('pdfjs-dist/package.json'));
const target = path.join(__dirname, '../public/pdfjs');
fs.mkdirSync(target, { recursive: true });
fs.copyFileSync(path.join(source, 'build/pdf.worker.min.mjs'), path.join(target, 'pdf.worker.min.mjs'));
for (const name of ['cmaps', 'standard_fonts', 'wasm']) {
  fs.cpSync(path.join(source, name), path.join(target, name), { recursive: true });
}
