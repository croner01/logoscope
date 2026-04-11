/**
 * 服务名规范化工具
 *
 * 目标：
 * 1. 统一前端展示/筛选的服务名；
 * 2. 当 service_name 实际上是 Pod 名时，自动剥离常见后缀；
 * 3. 与后端 logs facets 的服务归一化语义保持一致。
 */

const POD_SUFFIX_PATTERNS: RegExp[] = [
  /^(.+)-[a-f0-9]{8,10}-[a-z0-9]{5,10}$/i,
  /^(.+)-[a-f0-9]{8,10}(?:-[a-f0-9]{4,8})?$/i,
  /^(.+)-[a-z0-9]{5}$/i,
  /^(.+)-\d+$/,
];

function normalizeCandidate(value: unknown): string {
  const text = String(value || '').trim();
  if (!text) {
    return '';
  }
  return text.toLowerCase() === 'unknown' ? '' : text;
}

export function looksLikePodName(value: unknown): boolean {
  const text = normalizeCandidate(value);
  if (!text) {
    return false;
  }
  return POD_SUFFIX_PATTERNS.some((pattern) => pattern.test(text));
}

export function deriveServiceNameFromPodName(podName: unknown): string {
  const pod = normalizeCandidate(podName);
  if (!pod) {
    return '';
  }

  for (const pattern of POD_SUFFIX_PATTERNS) {
    const match = pod.match(pattern);
    if (match?.[1]) {
      return match[1];
    }
  }

  return pod;
}

/**
 * 解析事件级别 canonical service name。
 *
 * 规则：
 * - 优先使用 service_name；
 * - 当 service_name 明显是 Pod 名（或与 pod_name 相同）时做后缀剥离；
 * - service_name 不可用时，回退 pod_name 并做后缀剥离。
 */
export function resolveCanonicalServiceName(serviceName: unknown, podName?: unknown): string {
  const service = normalizeCandidate(serviceName);
  const pod = normalizeCandidate(podName);

  if (service) {
    if (service === pod || looksLikePodName(service)) {
      return deriveServiceNameFromPodName(service) || service;
    }
    return service;
  }

  if (pod) {
    return deriveServiceNameFromPodName(pod) || pod;
  }

  return 'unknown';
}
