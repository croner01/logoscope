/**
 * 格式化工具函数 - 参考 Datadog 设计风格
 */

/**
 * 格式化数字
 */
export function formatNumber(num: number): string {
  if (num >= 1e9) {
    return (num / 1e9).toFixed(2) + 'B';
  } else if (num >= 1e6) {
    return (num / 1e6).toFixed(2) + 'M';
  } else if (num >= 1e3) {
    return (num / 1e3).toFixed(2) + 'K';
  } else {
    return num.toString();
  }
}

/**
 * 格式化百分比
 */
export function formatPercent(value: number, decimals: number = 2): string {
  return `${value.toFixed(decimals)}%`;
}

/**
 * 格式化时间
 */
export function formatTime(timestamp: string): string {
  const date = parseTimestamp(timestamp);
  if (!date) {
    return '--';
  }
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function formatTimeWithTimezone(
  date: Date,
  options: { timeZone: string; suffix?: string }
): string {
  const formatter = new Intl.DateTimeFormat('zh-CN', {
    timeZone: options.timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
  const parts = formatter.formatToParts(date);
  const valueByType: Record<string, string> = {};
  parts.forEach((part) => {
    valueByType[part.type] = part.value;
  });
  const year = valueByType.year || '0000';
  const month = valueByType.month || '00';
  const day = valueByType.day || '00';
  const hour = valueByType.hour || '00';
  const minute = valueByType.minute || '00';
  const second = valueByType.second || '00';
  const suffixText = String(options.suffix || '').trim();
  return `${year}-${month}-${day} ${hour}:${minute}:${second}${suffixText ? ` ${suffixText}` : ''}`;
}

/**
 * 格式化时间（CST，Asia/Shanghai）
 */
export function formatTimeCST(timestamp: string): string {
  const date = parseTimestamp(timestamp);
  if (!date) {
    return '--';
  }
  return formatTimeWithTimezone(date, { timeZone: 'Asia/Shanghai' });
}

/**
 * 格式化时间（UTC）
 */
export function formatTimeUTC(timestamp: string): string {
  const date = parseTimestamp(timestamp);
  if (!date) {
    return '--';
  }
  return formatTimeWithTimezone(date, { timeZone: 'UTC', suffix: 'UTC' });
}

/**
 * 时间转 Unix 毫秒
 */
export function toEpochMs(timestamp: string): number {
  const date = parseTimestamp(timestamp);
  return date ? date.getTime() : 0;
}

/**
 * 解析多种时间戳格式（优先按 UTC 解析）
 */
export function parseTimestamp(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (value instanceof Date) {
    return Number.isFinite(value.getTime()) ? value : null;
  }
  if (typeof value === 'number') {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? date : null;
  }

  const raw = String(value).trim();
  if (!raw) {
    return null;
  }

  // 纯数字时间戳：支持秒/毫秒
  if (/^\d+$/.test(raw)) {
    const parsed = Number(raw);
    if (Number.isFinite(parsed)) {
      const millis = raw.length <= 10 ? parsed * 1000 : parsed;
      const date = new Date(millis);
      if (Number.isFinite(date.getTime())) {
        return date;
      }
    }
  }

  let normalized = raw;
  if (normalized.includes(' ') && !normalized.includes('T')) {
    normalized = normalized.replace(' ', 'T');
  }
  // Date 仅支持毫秒精度，截断更高精度的小数位
  normalized = normalized.replace(/(\.\d{3})\d+/, '$1');
  if (!/([zZ]|[+-]\d{2}:\d{2})$/.test(normalized)) {
    normalized = `${normalized}Z`;
  }

  let date = new Date(normalized);
  if (!Number.isFinite(date.getTime())) {
    // 兜底：尝试原始格式
    date = new Date(raw);
  }
  return Number.isFinite(date.getTime()) ? date : null;
}

/**
 * 格式化持续时间
 */
export function formatDuration(ms: number): string {
  const safeMs = Number(ms);
  if (!Number.isFinite(safeMs) || safeMs < 0) {
    return '0ms';
  }

  if (safeMs < 1000) {
    return `${safeMs}ms`;
  } else if (safeMs < 60000) {
    return `${(safeMs / 1000).toFixed(2)}s`;
  } else if (safeMs < 3600000) {
    return `${(safeMs / 60000).toFixed(2)}m`;
  } else {
    return `${(safeMs / 3600000).toFixed(2)}h`;
  }
}

/**
 * 格式化文件大小
 */
export function formatFileSize(bytes: number): string {
  if (bytes >= 1e9) {
    return (bytes / 1e9).toFixed(2) + 'GB';
  } else if (bytes >= 1e6) {
    return (bytes / 1e6).toFixed(2) + 'MB';
  } else if (bytes >= 1e3) {
    return (bytes / 1e3).toFixed(2) + 'KB';
  } else {
    return `${bytes}B`;
  }
}

/**
 * 格式化状态
 */
export function formatStatus(status: string): string {
  const statusMap: Record<string, string> = {
    healthy: '健康',
    degraded: '降级',
    critical: '严重',
    unknown: '未知',
    firing: '触发',
    resolved: '已解决',
  };
  return statusMap[status] || status;
}

/**
 * 格式化颜色
 */
export function formatColor(status: string): string {
  const colorMap: Record<string, string> = {
    healthy: '#4CAF50',
    degraded: '#FFC107',
    critical: '#F44336',
    unknown: '#9E9E9E',
    firing: '#F44336',
    resolved: '#4CAF50',
    warning: '#F59E0B',
    info: '#3B82F6',
    TRACE: '#6B7280',
    DEBUG: '#6366F1',
    INFO: '#3B82F6',
    WARN: '#F59E0B',
    ERROR: '#EF4444',
    FATAL: '#DC2626',
  };
  return colorMap[status] || '#9E9E9E';
}

/**
 * 格式化搜索查询
 */
export function formatQuery(query: string): string {
  return query.replace(/\s+/g, ' ').trim();
}

/**
 * 格式化 URL
 */
export function formatURL(url: string): string {
  return url.replace(/^https?:\/\//, '').replace(/\/.*$/, '');
}

/**
 * 格式化错误信息
 */
export function formatError(error: Error): string {
  return error.message || '未知错误';
}

/**
 * 格式化 JSON
 */
export function formatJSON(obj: any): string {
  return JSON.stringify(obj, null, 2);
}

/**
 * 格式化代码
 */
export function formatCode(code: string): string {
  return code.replace(/\t/g, '  ');
}

/**
 * 格式化文本
 */
export function formatText(text: string, maxLength: number = 100): string {
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + '...';
}

/**
 * 格式化数组
 */
export function formatArray(arr: any[], maxItems: number = 5): string {
  if (arr.length <= maxItems) return arr.join(', ');
  return arr.slice(0, maxItems).join(', ') + `... (${arr.length} 项)`;
}

/**
 * 格式化对象
 */
export function formatObject(obj: any, maxKeys: number = 5): string {
  const keys = Object.keys(obj);
  if (keys.length <= maxKeys) return JSON.stringify(obj, null, 2);
  const truncated = keys.slice(0, maxKeys).reduce((acc, key) => {
    acc[key] = obj[key];
    return acc;
  }, {} as any);
  return JSON.stringify(truncated, null, 2) + `... (${keys.length} 个键)`;
}

/**
 * 格式化日期范围
 */
export function formatDateRange(start: string, end: string): string {
  const startDate = new Date(start);
  const endDate = new Date(end);
  return `${startDate.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })} - ${endDate.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}`;
}

/**
 * 格式化时间窗口
 */
export function formatTimeWindow(window: string): string {
  const windowMap: Record<string, string> = {
    '1m': '1分钟',
    '5m': '5分钟',
    '15m': '15分钟',
    '30m': '30分钟',
    '1h': '1小时',
    '6h': '6小时',
    '12h': '12小时',
    '1d': '1天',
    '7d': '7天',
    '30d': '30天',
    '1 MINUTE': '1分钟',
    '5 MINUTE': '5分钟',
    '15 MINUTE': '15分钟',
    '30 MINUTE': '30分钟',
    '1 HOUR': '1小时',
    '6 HOUR': '6小时',
    '12 HOUR': '12小时',
    '24 HOUR': '24小时',
  };
  return windowMap[window] || window;
}

/**
 * 格式化版本号
 */
export function formatVersion(version: string): string {
  return version.replace(/^v/, '');
}

/**
 * 格式化 IP 地址
 */
export function formatIP(ip: string): string {
  return ip;
}

/**
 * 格式化端口号
 */
export function formatPort(port: number): string {
  return port.toString();
}

/**
 * 格式化路径
 */
export function formatPath(path: string): string {
  return path;
}

/**
 * 格式化查询参数
 */
export function formatQueryParams(params: Record<string, any>): string {
  return Object.entries(params)
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`)
    .join('&');
}

/**
 * 格式化 HTTP 状态码
 */
export function formatHTTPStatus(status: number): string {
  const statusMap: Record<number, string> = {
    200: 'OK',
    201: 'Created',
    202: 'Accepted',
    204: 'No Content',
    400: 'Bad Request',
    401: 'Unauthorized',
    403: 'Forbidden',
    404: 'Not Found',
    405: 'Method Not Allowed',
    409: 'Conflict',
    422: 'Unprocessable Entity',
    500: 'Internal Server Error',
    502: 'Bad Gateway',
    503: 'Service Unavailable',
    504: 'Gateway Timeout',
  };
  return statusMap[status] || `Status ${status}`;
}

/**
 * 格式化 HTTP 方法
 */
export function formatHTTPMethod(method: string): string {
  return method.toUpperCase();
}

/**
 * 格式化 HTTP 头
 */
export function formatHTTPHeaders(headers: Record<string, string>): string {
  return Object.entries(headers)
    .map(([key, value]) => `${key}: ${value}`)
    .join('\n');
}
