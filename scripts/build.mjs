import { copyFile, cp, mkdir, readFile, writeFile } from 'node:fs/promises';


const file = path => new URL(path, import.meta.url);
await mkdir(file('../dist'), { recursive: true });
await cp(file('../public'), file('../dist'), { recursive: true });
await copyFile(file('../src/twin.js'), file('../dist/twin.js'));
await copyFile(file('../src/tool-catalog.js'), file('../dist/tool-catalog.js'));

const [html, css, twin, catalog, flagging, databricks, ui] = await Promise.all([
  readFile(file('../public/index.html'), 'utf8'),
  readFile(file('../public/styles.css'), 'utf8'),
  readFile(file('../src/twin.js'), 'utf8'),
  readFile(file('../src/tool-catalog.js'), 'utf8'),
  readFile(file('../public/flagging.js'), 'utf8'),
  readFile(file('../public/databricks.js'), 'utf8'),
  readFile(file('../public/ui.js'), 'utf8'),
]);

// The standalone copy lets the operator launch the console without a listener.
function inlineModule(source) {
  const exports = source.match(/^export \{ ([^\n]+) \};$/m)?.[1];
  const body = source.replace(/^import \{[^\n]+\} from '[^'\n]+';\s*$/gm, '')
    .replace(/^export \{[^\n]+\};\s*$/gm, '');
  return exports ? `const { ${exports} } = (() => {\n${body}\nreturn { ${exports} };\n})();` : body;
}
const combined = [twin, catalog, flagging, databricks, ui].map(inlineModule).join('\n');
const standalone = html
  .replace('<link rel="stylesheet" href="./styles.css">', () => '<style>\n' + css + '</style>')
  .replace('<script type="module" src="./ui.js"></script>', () => '<script type="module">\n' + combined.replaceAll('</script', '<\\/script') + '</script>');
const standaloneFile = file('../dist/ai-infrastructure-optimisation-platform.html');
await writeFile(standaloneFile, standalone);
// Refresh the previous standalone filename for existing local bookmarks.
await copyFile(standaloneFile, file('../dist/Crosslayer.html'));
