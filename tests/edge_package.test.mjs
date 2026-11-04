import test from 'node:test';
import assert from 'node:assert/strict';
import {copyFileSync, mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

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
