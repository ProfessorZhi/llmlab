/*! tests/web-features/engines-library.test.js —— 引擎库（`#/engines`）与「服务跑在哪个 build 上」。
 *
 *  这一页回答的问题是「这台机器**装**了什么」，与「推理服务」页的「什么**在跑**」
 *  是两件事。这里钉住映射里的几处诚实性：
 *
 *    1. 合成的 ops 载荷不呈现为「本机装了这些」——夹具不是磁盘扫描结果；
 *    2. 可执行文件路径只有 owner 拿得到；别人的那一行照实说「仅所有者可见」，
 *       不画成「这个 build 没有路径」；
 *    3. 「服务跑在哪个 build 上」只在服务端给出结论时才显示；没有就**不加那一列**，
 *       而不是填一个「未关联」，那会被读成「它没有 build」。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { buildsFromOps, servicesFromOps } from '../../web/console/js/viewmodel.js';
import { validateOps } from '../../web/js/features/contract.js';
import { createMockHttp } from '../../web/js/features/mock-fixture.js';

function ops({ builds = [], engines = [], synthetic = false, attached = true } = {}) {
  return {
    synthetic,
    registry: { adapter_attached: attached, builds, engines, owned: [], all: [], shared: [] },
  };
}

const BUILD = {
  build_id: 'bld_1',
  framework: 'llama.cpp',
  version: 'b4120',
  found_as: 'executable',
  binary_path: '/usr/local/bin/llama-server',
  binary_path_sha256: 'deadbeef',
  binary_path_redacted: false,
  env: null,
  formats: [],
  owner: 'account:aaa',
  owner_name: 'alice',
  owner_is_self: true,
  state: 'present',
  last_seen_at: '2026-10-26T09:00:00+00:00',
};

test('引擎库：字段照抄服务端的观测，不补默认值', () => {
  const [row] = buildsFromOps(ops({ builds: [BUILD] }));

  assert.deepEqual(row, {
    id: 'bld_1',
    framework: 'llama.cpp',
    version: 'b4120',
    foundAs: 'executable',
    path: '/usr/local/bin/llama-server',
    redacted: false,
    env: null,
    owner: 'alice',
    self: true,
    state: 'present',
    lastSeenAt: '2026-10-26T09:00:00+00:00',
  });
});

test('别人名下的 build 不给路径，并且明确说是「打码」而不是「没有」', () => {
  const [row] = buildsFromOps(ops({
    builds: [{ ...BUILD, owner_is_self: false, binary_path: null, binary_path_redacted: true }],
  }));

  assert.equal(row.path, null);
  assert.equal(row.redacted, true);
});

test('没有 binary_path_redacted 字段时，有指纹就说明它被打码过', () => {
  const [row] = buildsFromOps(ops({
    builds: [{ ...BUILD, binary_path: null, binary_path_redacted: undefined }],
  }));

  assert.equal(row.redacted, true, '指纹在而路径不在，只可能是服务端打码');
});

test('合成的 ops 载荷不呈现为本机装了东西', () => {
  assert.deepEqual(buildsFromOps(ops({ builds: [BUILD], synthetic: true })), []);
});

test('适配器没接上时给 null（页面显示未知），不是空库', () => {
  assert.equal(buildsFromOps(ops({ attached: false })), null);
  assert.equal(buildsFromOps(undefined), null);
});

test('服务行带上它跑的 build：「框架 版本」', () => {
  const engine = {
    engine_id: 'eng_1', name: 'qwen-serve', protocol: 'openai_compatible',
    framework: 'llama.cpp', port: 8000, owner: 'account:aaa', owner_name: 'alice',
    owner_is_self: true, status: 'verified', run_as_user: 'alice',
    runtime_build_id: 'bld_1', last_seen_at: null,
  };
  const [row] = servicesFromOps(ops({ builds: [BUILD], engines: [engine] }));

  assert.equal(row.build, 'llama.cpp b4120');
});

test('服务端没有给出 build 关联时，服务行不给一个假值', () => {
  const engine = {
    engine_id: 'eng_1', name: 'qwen-serve', protocol: 'openai_compatible',
    framework: 'llama.cpp', port: 8000, owner: 'account:aaa', owner_name: 'alice',
    owner_is_self: true, status: 'verified', runtime_build_id: null, last_seen_at: null,
  };
  const [row] = servicesFromOps(ops({ engines: [engine] }));

  assert.equal(row.build, null);
});

/* ---------------- 浏览器契约：registry.builds ---------------- */

/** 一份真实的 `portal-ops/1`，只改 `registry.builds`：契约接受与否由它自己回答。
 *
 *  行由 `rows(subject)` 生成——`owner` 必须与会话主体对得上，这是契约的一半，
 *  所以不给它一个写死的 owner 让测试自己糊过去。
 */
function withBuilds(rows) {
  const payload = JSON.parse(
    JSON.stringify(createMockHttp({ persona: 'admin', visibility: 'shared' }).ops()),
  );
  payload.registry.builds = rows(payload.scope.subject);
  return validateOps(payload);
}

test('契约接受一行 build —— 包括「路径被打码、指纹还在」的形态', () => {
  const result = withBuilds((subject) => {
    const mine = { ...BUILD, owner: subject, owner_is_self: true };
    return [mine, { ...mine, build_id: 'bld_2', binary_path: null, binary_path_redacted: true }];
  });

  assert.deepEqual(result.errors, []);
  assert.equal(result.ok, true);
});

test('契约拒绝额外字段：白名单之外的键不许混进来', () => {
  const result = withBuilds(() => [{ ...BUILD, flags: {} }]);

  assert.equal(result.ok, false);
  assert.match(result.errors.join('\n'), /unexpected field/);
});

test('契约拒绝把别人的可执行文件路径带进载荷', () => {
  const result = withBuilds(() => [{ ...BUILD, owner: 'account:other', owner_is_self: false }]);

  assert.equal(result.ok, false);
  assert.match(result.errors.join('\n'), /another principal's binary path/);
});

test('契约拒绝把别人的虚拟环境带进载荷', () => {
  const result = withBuilds(() => [{
    ...BUILD, owner: 'account:other', owner_is_self: false,
    binary_path: null, binary_path_redacted: true, env: '/home/dave/venv',
  }]);

  assert.equal(result.ok, false);
  assert.match(result.errors.join('\n'), /virtual environment/);
});

test('契约拒绝 owner_is_self 与会话主体不一致的行', () => {
  const result = withBuilds(() => [{ ...BUILD, owner: 'account:other', owner_is_self: true }]);

  assert.equal(result.ok, false);
  assert.match(result.errors.join('\n'), /owner_is_self disagrees/);
});

test('build 不在可见列表里时退回 id，而不是「未关联」', () => {
  const engine = {
    engine_id: 'eng_1', name: 'qwen-serve', protocol: 'openai_compatible',
    framework: 'llama.cpp', port: 8000, owner: 'account:aaa', owner_name: 'alice',
    owner_is_self: true, status: 'verified', runtime_build_id: 'bld_other', last_seen_at: null,
  };
  const [row] = servicesFromOps(ops({ engines: [engine] }));

  assert.equal(row.build, 'bld_other');
});
