import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizeExecutableCommand,
  normalizeFollowUpCommandMatchKey,
  splitCommandLikeShlex,
} from '../.tmp-tests/followUpCommandMatch.js';

test('normalizeExecutableCommand strips prefixes and wrappers', () => {
  assert.equal(normalizeExecutableCommand('`kubectl get pods`'), 'kubectl get pods');
  assert.equal(normalizeExecutableCommand('- P1 执行命令: echo hi'), 'echo hi');
  assert.equal(normalizeExecutableCommand('  $ rg timeout app.log  '), 'rg timeout app.log');
});

test('splitCommandLikeShlex handles quotes and escapes', () => {
  assert.deepEqual(splitCommandLikeShlex('echo "a b"'), ['echo', 'a b']);
  assert.deepEqual(splitCommandLikeShlex("echo 'a b'"), ['echo', 'a b']);
  assert.deepEqual(splitCommandLikeShlex('echo a\\ b'), ['echo', 'a b']);
  assert.deepEqual(splitCommandLikeShlex('echo "a\\"b"'), ['echo', 'a"b']);
  assert.deepEqual(splitCommandLikeShlex('echo "a\\\nb"'), ['echo', 'a\\\nb']);
  assert.deepEqual(splitCommandLikeShlex('echo "C:\\\\logs\\\"prod\\\""'), ['echo', 'C:\\logs"prod"']);
  assert.deepEqual(splitCommandLikeShlex('echo "ab"\'cd\''), ['echo', 'abcd']);
  assert.deepEqual(splitCommandLikeShlex('echo "a\\xb"'), ['echo', 'a\\xb']);
});

test('splitCommandLikeShlex throws on unclosed quotes', () => {
  assert.throws(() => splitCommandLikeShlex('echo "a b'), /Unclosed quotation/);
  assert.throws(() => splitCommandLikeShlex('echo a\\'), /No escaped character/);
});

test('normalizeFollowUpCommandMatchKey aligns shell-equivalent commands', () => {
  const fromQuoted = normalizeFollowUpCommandMatchKey('echo "a b"');
  const fromEscaped = normalizeFollowUpCommandMatchKey('echo a\\ b');
  assert.equal(fromQuoted, fromEscaped);
});

test('normalizeFollowUpCommandMatchKey keeps semantic differences', () => {
  const singleSpace = normalizeFollowUpCommandMatchKey('echo "a b"');
  const tripleSpace = normalizeFollowUpCommandMatchKey('echo "a   b"');
  assert.notEqual(singleSpace, tripleSpace);
});

test('normalizeFollowUpCommandMatchKey falls back on parse error', () => {
  assert.equal(normalizeFollowUpCommandMatchKey('echo "a b'), 'echo "a b');
  assert.equal(normalizeFollowUpCommandMatchKey('echo a\\'), 'echo a\\');
});

test('control-symbol adjacency and escaping keep shell-token semantics', () => {
  assert.deepEqual(splitCommandLikeShlex('echo a|b'), ['echo', 'a|b']);
  assert.deepEqual(splitCommandLikeShlex('echo a\\|b'), ['echo', 'a|b']);
  assert.deepEqual(splitCommandLikeShlex('echo "a|b"'), ['echo', 'a|b']);
  assert.deepEqual(splitCommandLikeShlex("echo 'a|b'"), ['echo', 'a|b']);

  assert.deepEqual(splitCommandLikeShlex('echo a\\>b'), ['echo', 'a>b']);
  assert.deepEqual(splitCommandLikeShlex('echo "a>b"'), ['echo', 'a>b']);
  assert.deepEqual(splitCommandLikeShlex("echo 'a>b'"), ['echo', 'a>b']);
});

test('match key behavior around control symbols mirrors backend shlex split', () => {
  const compactPipe = normalizeFollowUpCommandMatchKey('echo a|b');
  const escapedPipe = normalizeFollowUpCommandMatchKey('echo a\\|b');
  const quotedPipe = normalizeFollowUpCommandMatchKey('echo "a|b"');
  const singleQuotedPipe = normalizeFollowUpCommandMatchKey("echo 'a|b'");
  const spacedPipe = normalizeFollowUpCommandMatchKey('echo a | b');

  assert.equal(compactPipe, escapedPipe);
  assert.equal(compactPipe, quotedPipe);
  assert.equal(compactPipe, singleQuotedPipe);
  assert.notEqual(compactPipe, spacedPipe);

  const compactAmp = normalizeFollowUpCommandMatchKey('echo a&b');
  const escapedAmp = normalizeFollowUpCommandMatchKey('echo a\\&b');
  const quotedAmp = normalizeFollowUpCommandMatchKey("echo 'a&b'");
  const spacedAmp = normalizeFollowUpCommandMatchKey('echo a & b');
  assert.equal(compactAmp, escapedAmp);
  assert.equal(compactAmp, quotedAmp);
  assert.notEqual(compactAmp, spacedAmp);
});

test('extended operator variants (|&, <&, >&, <>) follow shlex-like match behavior', () => {
  const compactPipeAmp = normalizeFollowUpCommandMatchKey('echo a|&b');
  const escapedPipeAmp = normalizeFollowUpCommandMatchKey('echo a\\|\\&b');
  const quotedPipeAmp = normalizeFollowUpCommandMatchKey('echo "a|&b"');
  const spacedPipeAmp = normalizeFollowUpCommandMatchKey('echo a |& b');
  assert.equal(compactPipeAmp, escapedPipeAmp);
  assert.equal(compactPipeAmp, quotedPipeAmp);
  assert.notEqual(compactPipeAmp, spacedPipeAmp);

  const compactInOutFd = normalizeFollowUpCommandMatchKey('echo a<&b');
  const escapedInOutFd = normalizeFollowUpCommandMatchKey('echo a\\<\\&b');
  const quotedInOutFd = normalizeFollowUpCommandMatchKey('echo "a<&b"');
  const spacedInOutFd = normalizeFollowUpCommandMatchKey('echo a <& b');
  assert.equal(compactInOutFd, escapedInOutFd);
  assert.equal(compactInOutFd, quotedInOutFd);
  assert.notEqual(compactInOutFd, spacedInOutFd);

  const compactOutFd = normalizeFollowUpCommandMatchKey('echo a>&b');
  const escapedOutFd = normalizeFollowUpCommandMatchKey('echo a\\>\\&b');
  const quotedOutFd = normalizeFollowUpCommandMatchKey('echo "a>&b"');
  const spacedOutFd = normalizeFollowUpCommandMatchKey('echo a >& b');
  assert.equal(compactOutFd, escapedOutFd);
  assert.equal(compactOutFd, quotedOutFd);
  assert.notEqual(compactOutFd, spacedOutFd);

  const compactReadWrite = normalizeFollowUpCommandMatchKey('echo a<>b');
  const escapedReadWrite = normalizeFollowUpCommandMatchKey('echo a\\<\\>b');
  const quotedReadWrite = normalizeFollowUpCommandMatchKey('echo "a<>b"');
  const spacedReadWrite = normalizeFollowUpCommandMatchKey('echo a <> b');
  assert.equal(compactReadWrite, escapedReadWrite);
  assert.equal(compactReadWrite, quotedReadWrite);
  assert.notEqual(compactReadWrite, spacedReadWrite);
});

test('redirection-family variants keep shlex-like match semantics', () => {
  assert.deepEqual(splitCommandLikeShlex('echo ok 2>/tmp/x'), ['echo', 'ok', '2>/tmp/x']);
  assert.deepEqual(splitCommandLikeShlex('echo ok 2> /tmp/x'), ['echo', 'ok', '2>', '/tmp/x']);
  assert.deepEqual(splitCommandLikeShlex('echo 2>&1'), ['echo', '2>&1']);
  assert.deepEqual(splitCommandLikeShlex('echo >|'), ['echo', '>|']);
  assert.deepEqual(splitCommandLikeShlex('echo <<<'), ['echo', '<<<']);

  assert.equal(
    normalizeFollowUpCommandMatchKey('echo ok 2>/tmp/x'),
    normalizeFollowUpCommandMatchKey('echo ok 2\\>/tmp/x'),
  );
  assert.notEqual(
    normalizeFollowUpCommandMatchKey('echo ok 2>/tmp/x'),
    normalizeFollowUpCommandMatchKey('echo ok 2> /tmp/x'),
  );

  assert.equal(
    normalizeFollowUpCommandMatchKey('echo 2\\>\\&1'),
    normalizeFollowUpCommandMatchKey('echo "2>&1"'),
  );
  assert.notEqual(
    normalizeFollowUpCommandMatchKey('echo 2>&1'),
    normalizeFollowUpCommandMatchKey('echo 2 >& 1'),
  );

  assert.equal(
    normalizeFollowUpCommandMatchKey('echo \\>\\|'),
    normalizeFollowUpCommandMatchKey('echo ">|"'),
  );
  assert.equal(
    normalizeFollowUpCommandMatchKey('echo \\<\\<\\<'),
    normalizeFollowUpCommandMatchKey('echo "<<<"'),
  );
});

test('unicode whitespace is not a shell separator for shlex-like split', () => {
  assert.deepEqual(splitCommandLikeShlex('echo\u00A0hi'), ['echo\u00A0hi']);
  assert.deepEqual(splitCommandLikeShlex('echo\u3000hi'), ['echo\u3000hi']);
  assert.deepEqual(splitCommandLikeShlex('echo\u2003hi'), ['echo\u2003hi']);
});

test('unicode whitespace changes match key semantics', () => {
  const asciiSpace = normalizeFollowUpCommandMatchKey('echo hi');
  const nbspSpace = normalizeFollowUpCommandMatchKey('echo\u00A0hi');
  const ideographicSpace = normalizeFollowUpCommandMatchKey('echo\u3000hi');
  assert.notEqual(asciiSpace, nbspSpace);
  assert.notEqual(asciiSpace, ideographicSpace);
  assert.equal(normalizeExecutableCommand('\u3000echo hi\u3000'), 'echo hi');
});
