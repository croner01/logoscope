/**
 * Runtime command_spec builders for structured command execution.
 */

type RuntimeCommandSpecParams = {
  command: string;
  targetKind?: string;
  targetIdentity?: string;
  timeoutSeconds?: number;
  purpose?: string;
  title?: string;
  stepId?: string;
};

const asText = (value: unknown): string => (
  typeof value === 'string' ? value : value === null || value === undefined ? '' : String(value)
);

const splitCommandArgs = (command: string): string[] => {
  const text = asText(command).trim();
  if (!text) {
    return [];
  }
  const tokens: string[] = [];
  const pattern = /"([^"\\]*(?:\\.[^"\\]*)*)"|'([^'\\]*(?:\\.[^'\\]*)*)'|[^\s]+/g;
  let match: RegExpExecArray | null = pattern.exec(text);
  while (match) {
    const token = String(match[1] || match[2] || match[0] || '').trim();
    if (token) {
      tokens.push(token);
    }
    match = pattern.exec(text);
  }
  return tokens;
};

const extractFlagValue = (argv: string[], shortFlag: string, longFlag: string): string => {
  for (let i = 0; i < argv.length; i += 1) {
    const token = String(argv[i] || '').trim();
    if (!token) {
      continue;
    }
    if ((shortFlag && token === shortFlag) || (longFlag && token === longFlag)) {
      const next = String(argv[i + 1] || '').trim();
      if (next && !next.startsWith('-')) {
        return next;
      }
      continue;
    }
    if (longFlag && token.startsWith(`${longFlag}=`)) {
      return String(token.slice(longFlag.length + 1)).trim();
    }
    if (shortFlag && token.startsWith(shortFlag) && token.length > shortFlag.length) {
      const compact = String(token.slice(shortFlag.length)).trim();
      if (compact && !compact.startsWith('-')) {
        return compact;
      }
    }
  }
  return '';
};

const inferTargetFromCommand = (command: string): { targetKind?: string; targetIdentity?: string } => {
  const argv = splitCommandArgs(command);
  const head = String(argv[0] || '').trim().toLowerCase();
  if (!head) {
    return {};
  }
  if (head === 'kubectl') {
    const namespace = extractFlagValue(argv, '-n', '--namespace').toLowerCase();
    if (namespace) {
      return { targetKind: 'k8s_cluster', targetIdentity: `namespace:${namespace}` };
    }
    return { targetKind: 'k8s_cluster', targetIdentity: 'cluster:kubernetes' };
  }
  if (head === 'openstack') {
    const cloud = extractFlagValue(argv, '', '--os-cloud');
    return { targetKind: 'openstack_cluster', targetIdentity: cloud ? `cloud:${cloud}` : 'cloud:default' };
  }
  if (head === 'clickhouse-client' || head === 'clickhouse') {
    const database = extractFlagValue(argv, '-d', '--database');
    return { targetKind: 'clickhouse_cluster', targetIdentity: database ? `database:${database}` : 'database:default' };
  }
  if (head === 'psql' || head === 'postgres') {
    const database = extractFlagValue(argv, '-d', '--dbname') || extractFlagValue(argv, '', '--database');
    return { targetKind: 'postgres_cluster', targetIdentity: database ? `database:${database}` : 'database:default' };
  }
  if (head === 'mysql' || head === 'mariadb') {
    const database = extractFlagValue(argv, '-D', '--database');
    return { targetKind: 'mysql_cluster', targetIdentity: database ? `database:${database}` : 'database:default' };
  }
  return {};
};

const clampTimeoutSeconds = (value: unknown, fallback = 20): number => {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }
  return Math.max(3, Math.min(180, Math.floor(parsed)));
};

export const resolveRuntimeClientDeadlineMs = (timeoutMs = 180000): number => {
  const parsed = Number(timeoutMs);
  const budgetMs = Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 180000;
  return Date.now() + Math.max(10000, budgetMs);
};

export const buildRuntimeCommandSpec = (params: RuntimeCommandSpecParams): Record<string, unknown> => {
  const command = asText(params.command).trim();
  const timeoutSeconds = clampTimeoutSeconds(params.timeoutSeconds, 20);
  const inferredTarget = inferTargetFromCommand(command);
  const targetKind = asText(params.targetKind).trim() || inferredTarget.targetKind || '';
  const targetIdentity = asText(params.targetIdentity).trim() || inferredTarget.targetIdentity || '';
  return {
    tool: 'generic_exec',
    args: {
      command,
      timeout_s: timeoutSeconds,
      target_kind: targetKind || undefined,
      target_identity: targetIdentity || undefined,
    },
    command,
    timeout_s: timeoutSeconds,
    target_kind: targetKind || undefined,
    target_identity: targetIdentity || undefined,
    step_id: asText(params.stepId).trim() || undefined,
    purpose: asText(params.purpose).trim() || undefined,
    title: asText(params.title).trim() || undefined,
  };
};

export const buildRuntimePipelineSteps = (params: RuntimeCommandSpecParams): Array<Record<string, unknown>> => {
  const command = asText(params.command).trim();
  if (!command) {
    return [];
  }
  const stepId = asText(params.stepId).trim() || 'step-1';
  const inferredTarget = inferTargetFromCommand(command);
  const targetHint = asText(params.targetIdentity).trim() || inferredTarget.targetIdentity || '';
  return [
    {
      step_id: stepId,
      intent: 'execute_structured_command',
      risk: 'unknown',
      target_hint: targetHint || 'unknown',
      command_spec: buildRuntimeCommandSpec({ ...params, stepId }),
    },
  ];
};
