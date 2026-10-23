/*! tests/web-features/modules-parse.test.js — 工作树里的每个模块都必须先能解析。
 *
 *  `tests/tracked-assets.test.js` 已经从 `git archive HEAD` 里 import 过一遍所有模块，
 *  但它看的是**已提交**的树。这次故障正好落在它的盲区：`gateway.js` 的 `draw()`
 *  少了一个右花括号，`await` 因此落在非 async 函数里；工作树里 npm test 全绿
 *  （测试读的是 HEAD），提交之后整棵前端模块图才一起解析失败 —— 网关下的页面
 *  全部打不开，而当时 Python 与 Node 的其它测试仍然是绿的。
 *
 *  所以这里补的是另一半：把**即将提交的内容**交给解析器，在落盘之前就失败。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

function modulesUnder(root) {
  const out = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const abs = join(dir, entry.name);
      if (entry.isDirectory()) walk(abs);
      else if (/\.m?js$/.test(entry.name)) out.push(abs);
    }
  };
  walk(root);
  return out.sort();
}

const FILES = [
  ...modulesUnder(join(REPO, 'web', 'console', 'js')),
  ...modulesUnder(join(REPO, 'web', 'js')),
  join(REPO, 'web', 'tools', 'serve.mjs'),
];

test('every module in the working tree parses', () => {
  assert.ok(FILES.length > 20, `只找到 ${FILES.length} 个模块，扫描路径可能写错了`);
  const broken = [];
  for (const file of FILES) {
    // `--check` 只解析、不执行：不需要 DOM，也不会碰网络。
    const r = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' });
    if (r.status !== 0) broken.push(`${relative(REPO, file)}\n${(r.stderr || '').split('\n').slice(0, 4).join('\n')}`);
  }
  assert.deepEqual(broken, [], `无法解析的模块：\n${broken.join('\n\n')}`);
});
