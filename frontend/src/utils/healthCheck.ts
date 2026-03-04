/**
 * 健康检查日志判定工具
 *
 * 目标：
 * 1. 过滤典型 kube/HTTP 健康探针日志；
 * 2. 避免因为宽泛关键词（如 health/ping）导致误杀业务日志。
 */

const HEALTH_CHECK_REGEX_PATTERNS: RegExp[] = [
  /\bkube-probe\b/i,
  /"(?:GET|HEAD)\s+\/health(?:z)?(?:\?[^"\s]*)?\s+HTTP\/1\.[01]"/i,
  /"(?:GET|HEAD)\s+\/(?:ready|readiness|live|liveness)(?:\?[^"\s]*)?\s+HTTP\/1\.[01]"/i,
  /\b(?:readiness|liveness)[\s_-]*probe\b/i,
];

export function isHealthCheckMessage(message: string): boolean {
  const normalized = String(message || '').trim();
  if (!normalized) {
    return false;
  }
  return HEALTH_CHECK_REGEX_PATTERNS.some((pattern) => pattern.test(normalized));
}
