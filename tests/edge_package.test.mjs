import test from 'node:test';
import assert from 'node:assert/strict';
import {copyFileSync, mkdtempSync, readFileSync, readdirSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

test('actual deployment folder contains only the entrypoint and current source files', async()=>{
  const directory = new URL('../supabase/functions/street-face-publish-v2/', import.meta.url);
  assert.deepEqual(readdirSync(directory).sort(),
    ['collection-artwork-verification.mjs', 'collection-publish-edge.ts', 'index.ts']);
  for (const name of ['collection-publish-edge.ts', 'collection-artwork-verification.mjs'])
    assert.deepEqual(readFileSync(new URL(name, directory)), readFileSync(new URL('../'+name, import.meta.url)));
  const entry = readFileSync(new URL('index.ts', directory), 'utf8');
  assert.equal(entry.trim(), "import './collection-publish-edge.ts';");
  const edge = readFileSync(new URL('collection-publish-edge.ts', directory), 'utf8');
  const relative = [...edge.matchAll(/from\s+['"](\.[^'"]+)['"]/g)];
  assert.deepEqual(relative.map(match=>match[1]), ['./collection-artwork-verification.mjs']);
  const helper = await import(new URL(relative[0][1], directory).href);
  assert.equal(typeof helper.verifyArtwork, 'function');
  const config = readFileSync(new URL('../supabase/config.toml', import.meta.url), 'utf8');
  assert.match(config, /project_id = "ngmdsmmvdnbnxeozpfej"/);
  assert.match(config, /\[functions\.street-face-publish-v2\]\s+verify_jwt = false/);
});

test('deployment source includes the relative artwork helper beside the Edge entry point',async()=>{
  const directory=mkdtempSync(join(tmpdir(),'street-face-edge-package-'));
  try {
    // These are the documented deployment source files; no import rewriting here.
    for(const name of ['collection-publish-edge.ts','collection-artwork-verification.mjs'])
      copyFileSync(new URL('../'+name,import.meta.url),join(directory,name));
    const entry=pathToFileURL(join(directory,'collection-publish-edge.ts'));
    const source=readFileSync(entry,'utf8');
    const imports=[...source.matchAll(/from\s+['"](\.[^'"]+)['"]/g)];
    assert.equal(imports.length,1);
    assert.equal(imports[0][1],'./collection-artwork-verification.mjs');
    const helper=await import(new URL(imports[0][1],entry).href);
    assert.equal(typeof helper.verifyArtwork,'function');
  } finally {
    rmSync(directory,{recursive:true,force:true});
  }
});
