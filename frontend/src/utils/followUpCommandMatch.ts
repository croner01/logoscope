/**
 * Follow-up command normalization and matching helpers.
 * Keep behavior aligned with backend command matching (shlex-like split + token key).
 */
const FOLLOWUP_COMMAND_HEADS = [
  'clickhouse-client',
  'clickhouse',
  'kubectl',
  'curl',
  'rg',
  'grep',
  'cat',
  'tail',
  'head',
  'awk',
  'jq',
  'ls',
  'echo',
  'pwd',
  'sed',
  'helm',
  'systemctl',
  'service',
];

const escapeRegex = (text: string): string => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const normalizeFollowUpCommandSpacing = (raw: string): string => {
  let normalized = String(raw || '');
  for (const head of FOLLOWUP_COMMAND_HEADS) {
    const headPattern = new RegExp(`^(${escapeRegex(head)})(?=(?:--|-)[A-Za-z])`, 'i');
    if (headPattern.test(normalized)) {
      normalized = normalized.replace(headPattern, '$1 ');
      break;
    }
  }
  normalized = normalized.replace(/(^|\s)(--[A-Za-z][\w-]*|-{1}[A-Za-z])(?=(["']))/g, '$1$2 ');
  return normalized;
};

export const normalizeExecutableCommand = (line: string): string => {
  let raw = String(line || '').trim();
  if (!raw) {
    return '';
  }
  if (raw.startsWith('`') && raw.endsWith('`') && raw.length > 2) {
    raw = raw.slice(1, -1).trim();
  }
  raw = raw.replace(/^\s*(?:[-*•]\s+|\d+\.\s+)/, '');
  raw = raw.replace(/^\s*P\d+\s+/i, '');
  raw = raw.replace(/^\s*(?:执行命令|命令)\s*[:：]\s*/, '');
  raw = raw.startsWith('$') ? raw.slice(1).trim() : raw;
  return normalizeFollowUpCommandSpacing(raw).trim();
};

export const splitCommandLikeShlex = (command: string): string[] => {
  const text = String(command || '');
  const tokens: string[] = [];
  let current = '';
  let tokenStarted = false;
  let mode: 'plain' | 'single' | 'double' = 'plain';
  const isShlexWhitespace = (char: string): boolean =>
    char === ' ' || char === '\t' || char === '\r' || char === '\n';

  const flush = () => {
    if (!tokenStarted) {
      return;
    }
    tokens.push(current);
    current = '';
    tokenStarted = false;
  };

  let index = 0;
  while (index < text.length) {
    const char = text[index];

    if (mode === 'plain') {
      if (isShlexWhitespace(char)) {
        flush();
        index += 1;
        continue;
      }
      if (char === "'") {
        mode = 'single';
        tokenStarted = true;
        index += 1;
        continue;
      }
      if (char === '"') {
        mode = 'double';
        tokenStarted = true;
        index += 1;
        continue;
      }
      if (char === '\\') {
        tokenStarted = true;
        if (index + 1 >= text.length) {
          throw new Error('No escaped character');
        }
        current += text[index + 1];
        index += 2;
        continue;
      }
      current += char;
      tokenStarted = true;
      index += 1;
      continue;
    }

    if (mode === 'single') {
      if (char === "'") {
        mode = 'plain';
        index += 1;
        continue;
      }
      current += char;
      tokenStarted = true;
      index += 1;
      continue;
    }

    if (char === '"') {
      mode = 'plain';
      index += 1;
      continue;
    }
    if (char === '\\') {
      const nextChar = text[index + 1];
      tokenStarted = true;
      if (!nextChar) {
        current += '\\';
        index += 1;
        continue;
      }
      if (nextChar === '\\' || nextChar === '"' || nextChar === '$' || nextChar === '`' || nextChar === '\n') {
        if (nextChar === '\n') {
          current += `\\${nextChar}`;
        } else {
          current += nextChar;
        }
        index += 2;
        continue;
      }
      current += '\\';
      index += 1;
      continue;
    }
    current += char;
    tokenStarted = true;
    index += 1;
  }

  if (mode !== 'plain') {
    throw new Error('Unclosed quotation in command');
  }
  flush();
  return tokens;
};

export const normalizeFollowUpCommandMatchKey = (command: string): string => {
  const normalized = normalizeExecutableCommand(command);
  if (!normalized) {
    return '';
  }
  try {
    const tokens = splitCommandLikeShlex(normalized);
    if (!tokens.length) {
      return '';
    }
    return tokens.join('\x1f');
  } catch (_error) {
    return normalized.replace(/\s+/g, ' ').trim();
  }
};
