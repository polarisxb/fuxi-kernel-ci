// Host-only tests of the actual shipped WebUI functions and logging fragment.
// Override SUSFS_MODULE_ROOT to run these assertions against the preserved version.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const moduleRoot = process.env.SUSFS_MODULE_ROOT || path.resolve(__dirname, '../../module/susfs4ksu');
const source = fs.readFileSync(path.join(moduleRoot, 'webroot/assets/index-BS9hEn4H.js'), 'utf8');
const service = fs.readFileSync(path.join(moduleRoot, 'service.sh'), 'utf8');

function between(start, end) {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `Production boundary missing: ${start}`);
  return source.slice(a, b);
}

const bridgeFunctions = 'let Se=0;' + between('function Ee(s)', 'function _(s)')
  + between('async function e(s)', 'function A(s)');
const settingsFunctions = between('function A(s)', 'function Ae(s)')
  + between('async function Y(s)', 'async function q(s)');

function bridgeContext(exec) {
  const context = vm.createContext({
    window: {}, ksu: { exec },
    Date: { now: () => 1000 },
    console: { error() {}, log() {} },
    Error, String, Number, Promise,
  });
  vm.runInContext(bridgeFunctions, context);
  return context;
}

test('same-millisecond commands retain distinct callbacks and correct out-of-order results', async () => {
  const requests = [];
  const context = bridgeContext((command, options, callback) => requests.push({ command, callback }));
  const settled = [];
  context.e('first').then(value => settled.push(['first', value]));
  context.e('second').then(value => settled.push(['second', value]));
  assert.notEqual(requests[0].callback, requests[1].callback);
  context.window[requests[1].callback](0, 'second-output', '');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(settled, [['second', 'second-output']]);
  context.window[requests[0].callback](0, 'first-output', '');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(settled, [['second', 'second-output'], ['first', 'first-output']]);
  assert.deepEqual(Object.keys(context.window), []);
});

test('nonzero native result rejects its own promise and cleans the callback', async () => {
  const requests = [];
  const context = bridgeContext((command, options, callback) => requests.push(callback));
  const result = context.e('failure');
  context.window[requests[0]](7, '', 'expected failure');
  await assert.rejects(result, error => String(error).includes('expected failure'));
  assert.deepEqual(Object.keys(context.window), []);
});

test('a synchronous bridge exception rejects and cleans the callback', async () => {
  const context = bridgeContext(() => { throw new Error('bridge unavailable'); });
  await assert.rejects(context.e('failure'), /bridge unavailable/);
  assert.deepEqual(Object.keys(context.window), []);
});

function tempFixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'susfs-module-test-'));
  const helper = path.join(directory, 'ksu_susfs');
  const config = path.join(directory, 'config.sh');
  const calls = path.join(directory, 'calls');
  fs.writeFileSync(helper, '#!/bin/sh\nprintf "%s\\0" "$@" >> "$CALLS_FILE"\n[ "${FAIL_TOOL:-0}" = 0 ]\n');
  fs.chmodSync(helper, 0o700);
  fs.writeFileSync(config, "unrelated=17\nkernel_version='default'\nkernel_build='default'\n");
  return { directory, helper, config, calls, cleanup: () => fs.rmSync(directory, { recursive: true }) };
}

function loggingFragment() {
  const match = service.match(/## (?:Disable|Apply) susfs kernel log ##([\s\S]*?)# SUSFS Logging/);
  assert.ok(match, 'Production logging fragment missing');
  return match[1];
}

for (const value of ['0', '1']) {
  test(`service applies explicit SUSFS logging state ${value}`, () => {
    const fixture = tempFixture();
    try {
      const result = spawnSync('sh', ['-c', loggingFragment()], {
        env: { ...process.env, SUSFS_BIN: fixture.helper, susfs_log: value, CALLS_FILE: fixture.calls }, encoding: 'utf8',
      });
      assert.equal(result.status, 0, result.stderr);
      assert.deepEqual(fs.existsSync(fixture.calls) ? fs.readFileSync(fixture.calls, 'utf8').split('\0').slice(0, -1) : [], ['enable_log', value]);
    } finally { fixture.cleanup(); }
  });
}

test('invalid logging values use the existing safe default rather than expanding shell input', () => {
  const fixture = tempFixture();
  try {
    for (const value of ['', '2', 'unexpected', '0 1', '$(touch should-not-exist)']) {
      fs.rmSync(fixture.calls, { force: true });
      const result = spawnSync('sh', ['-c', loggingFragment()], {
        env: { ...process.env, SUSFS_BIN: fixture.helper, susfs_log: value, CALLS_FILE: fixture.calls },
        cwd: fixture.directory, encoding: 'utf8',
      });
      assert.equal(result.status, 0, result.stderr);
      assert.deepEqual(fs.existsSync(fixture.calls) ? fs.readFileSync(fixture.calls, 'utf8').split('\0').slice(0, -1) : [], ['enable_log', '1']);
      assert.equal(fs.existsSync(path.join(fixture.directory, 'should-not-exist')), false);
    }
  } finally { fixture.cleanup(); }
});

function element() {
  let text = '';
  return {
    value: '', checked: false, disabled: false, style: {}, listeners: {},
    get innerHTML() { return text; }, set innerHTML(value) { text = String(value); },
    get textContent() { return text; }, set textContent(value) { text = String(value); },
    classList: { add() {}, remove() {} },
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
    setAttribute(name) { if (name === 'checked') this.checked = true; },
    removeAttribute(name) { if (name === 'checked') this.checked = false; },
    blur() {}, scrollIntoView() {}, showModal() {}, close() {},
    async trigger(name) { for (const callback of this.listeners[name] || []) await callback({ preventDefault() {} }); },
  };
}

async function settingsFixture({ failTool = false, failPersistence = false } = {}) {
  const fixture = tempFixture();
  const bin = path.join(fixture.directory, 'bin');
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, 'sed'), '#!/bin/sh\n[ "${FAIL_PERSISTENCE:-0}" = 0 ] || { echo "simulated write failure" >&2; exit 1; }\nexec /usr/bin/sed "$@"\n');
  fs.chmodSync(path.join(bin, 'sed'), 0o700);
  const elements = new Map();
  const node = name => {
    if (!elements.has(name)) elements.set(name, element());
    return elements.get(name);
  };
  const commands = [], toasts = [];
  let context;
  const exec = (command, options, callback) => {
    commands.push(command);
    let result;
    if (command === 'uname -r + uname -v') result = { status: 0, stdout: 'current-kernel', stderr: '' };
    else if (command === 'getprop ro.build.date') result = { status: 0, stdout: 'Mon Nov 9 12:00:00 CST 2026\n', stderr: '' };
    else result = spawnSync('sh', ['-c', command], {
      cwd: fixture.directory,
      env: { ...process.env, PATH: bin + ':' + process.env.PATH, CALLS_FILE: fixture.calls,
        FAIL_TOOL: failTool ? '1' : '0', FAIL_PERSISTENCE: failPersistence ? '1' : '0' }, encoding: 'utf8',
    });
    // Synchronous completion isolates quoting tests from the separately tested callback race.
    context.window[callback](result.status, result.stdout, result.stderr);
  };
  context = bridgeContext(exec);
  Object.assign(context, {
    k: fixture.helper, c: fixture.directory,
    _: message => toasts.push(message),
    document: { getElementById: node, querySelector: () => node('main') },
    setTimeout, R: { to() {} },
  });
  vm.runInContext(settingsFunctions, context);
  const settings = { kernel_version: 'default', kernel_build: 'default', spoof_uname: 0 };
  await context.Y(settings);
  return { ...fixture, context, settings, node, commands, toasts };
}

function persistedValues(fixture) {
  const result = spawnSync('sh', ['-c', '. "$1"; printf "%s\\0%s\\0%s" "$kernel_version" "$kernel_build" "$unrelated"', 'test', fixture.config], {
    cwd: fixture.directory, encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.split('\0');
}

for (const [name, build] of [
  ['ampersand', '#1 SMP & PREEMPT'],
  ['slashes', 'builder/android/kernel'],
  ['single quote', "builder's kernel"],
  ['double quote', 'build "quoted"'],
  ['backslash', 'builder\\kernel'],
  ['dollar expansion', '$(touch injected-dollar)'],
  ['backticks', '`touch injected-backtick`'],
  ['mixed quotes', "'\"&/\\$`"],
]) {
  test(`uname ${name} roundtrips as data through native arguments, shell config and WebUI parser`, async () => {
    const fixture = await settingsFixture();
    try {
      const release = '6.1.99-fixture';
      fixture.node('sus_uname_checkbox').checked = true;
      fixture.node('sus_uname_build_checkbox').checked = true;
      fixture.node('sus_uname').value = release;
      fixture.node('sus_uname_build').value = build;
      await fixture.node('set_uname').trigger('click');
      assert.deepEqual(persistedValues(fixture), [release, build, '17']);
      const parsed = fixture.context.A(fs.readFileSync(fixture.config, 'utf8'));
      assert.equal(parsed.kernel_version, release);
      assert.equal(parsed.kernel_build, build);
      assert.equal(fixture.settings.kernel_version, release);
      assert.equal(fixture.settings.kernel_build, build);
      assert.deepEqual(fs.readFileSync(fixture.calls, 'utf8').split('\0').slice(0, -1), ['set_uname', release, build]);
      assert.equal(fs.existsSync(path.join(fixture.directory, 'injected-dollar')), false);
      assert.equal(fs.existsSync(path.join(fixture.directory, 'injected-backtick')), false);
    } finally { fixture.cleanup(); }
  });
}

for (const [name, value] of [['newline', 'one\ntwo'], ['carriage return', 'one\rtwo'], ['NUL', 'one\0two']]) {
  test(`uname rejects ${name} without executing or persisting input`, async () => {
    const fixture = await settingsFixture();
    try {
      const before = fs.readFileSync(fixture.config, 'utf8');
      fixture.node('sus_uname_build_checkbox').checked = true;
      fixture.node('sus_uname_build').value = value;
      await fixture.node('set_uname').trigger('click');
      assert.equal(fs.readFileSync(fixture.config, 'utf8'), before);
      assert.equal(fs.existsSync(fixture.calls), false);
      assert.equal(fixture.settings.kernel_build, 'default');
      assert.ok(fixture.toasts.some(message => /control|line|invalid/i.test(message)));
    } finally { fixture.cleanup(); }
  });
}

test('selected empty values reset to defaults while unselected fields are preserved', async () => {
  const fixture = await settingsFixture();
  try {
    fixture.node('sus_uname_build_checkbox').checked = true;
    fixture.node('sus_uname_build').value = 'changed';
    await fixture.node('set_uname').trigger('click');
    assert.deepEqual(persistedValues(fixture), ['default', 'changed', '17']);
    fixture.node('sus_uname_build').value = '';
    await fixture.node('set_uname').trigger('click');
    assert.deepEqual(persistedValues(fixture), ['default', 'default', '17']);
  } finally { fixture.cleanup(); }
});

test('stock build date uses the same safe persistence and drops command output newline', async () => {
  const fixture = await settingsFixture();
  try {
    await fixture.node('set_stock_kernel_build_date').trigger('click');
    assert.deepEqual(persistedValues(fixture), ['default', '#1 SMP PREEMPT Mon Nov 9 12:00:00 CST 2026', '17']);
  } finally { fixture.cleanup(); }
});

test('boot-time uname application preserves whitespace, quotes and metacharacters without shell diagnostics', () => {
  const fixture = tempFixture();
  try {
    const utils = fs.readFileSync(path.join(moduleRoot, 'utils.sh'), 'utf8');
    const start = utils.indexOf('spoof_uname() {');
    assert.ok(start >= 0);
    const fragment = utils.slice(start, utils.indexOf('\n}', start) + 2);
    const release = "6.1.99-'quoted'";
    const build = "#1 & / ' \" * $(touch injected-boot) `touch injected-boot2`";
    const result = spawnSync('sh', ['-c', fragment + '\nspoof_uname\n'], {
      cwd: fixture.directory,
      env: { ...process.env, SUSFS_BIN: fixture.helper, kernel_version: release, kernel_build: build, CALLS_FILE: fixture.calls },
      encoding: 'utf8',
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stderr, '');
    assert.deepEqual(fs.readFileSync(fixture.calls, 'utf8').split('\0').slice(0, -1), ['set_uname', release, build]);
    assert.equal(fs.existsSync(path.join(fixture.directory, 'injected-boot')), false);
    assert.equal(fs.existsSync(path.join(fixture.directory, 'injected-boot2')), false);
  } finally { fixture.cleanup(); }
});

for (const option of ['failTool', 'failPersistence']) {
  test(`${option} does not change cached settings or report successful saving`, async () => {
    const fixture = await settingsFixture({ [option]: true });
    try {
      fixture.node('sus_uname_checkbox').checked = true;
      fixture.node('sus_uname_build_checkbox').checked = true;
      fixture.node('sus_uname').value = '6.1.99-fixture';
      fixture.node('sus_uname_build').value = 'new-build';
      await fixture.node('set_uname').trigger('click');
      assert.deepEqual(persistedValues(fixture), ['default', 'default', '17']);
      assert.equal(fixture.settings.kernel_version, 'default');
      assert.equal(fixture.settings.kernel_build, 'default');
      assert.ok(fixture.toasts.some(message => /failed/i.test(message)));
      assert.equal(fixture.node('set_uname').disabled, false);
      assert.equal(fixture.node('set_stock_kernel_build_date').disabled, false);
    } finally { fixture.cleanup(); }
  });
}
