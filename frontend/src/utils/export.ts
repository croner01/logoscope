/**
 * 数据导出工具
 * 
 * 支持多种格式导出：
 * - CSV
 * - JSON
 * - Excel (需要额外库)
 */

export interface ExportOptions {
  format: 'csv' | 'json';
  filename?: string;
  includeHeaders?: boolean;
}

export function exportToCSV(
  data: Record<string, any>[],
  columns: string[],
  filename: string = 'export.csv'
): void {
  const headers = columns.join(',');
  
  const rows = data.map(item => {
    return columns.map(col => {
      const value = item[col];
      if (value === null || value === undefined) {
        return '';
      }
      const stringValue = String(value);
      if (stringValue.includes(',') || stringValue.includes('"') || stringValue.includes('\n')) {
        return `"${stringValue.replace(/"/g, '""')}"`;
      }
      return stringValue;
    }).join(',');
  });

  const csv = [headers, ...rows].join('\n');
  
  downloadFile(csv, filename, 'text/csv;charset=utf-8;');
}

export function exportToJSON(
  data: Record<string, any>[] | Record<string, any>,
  filename: string = 'export.json'
): void {
  const json = JSON.stringify(data, null, 2);
  downloadFile(json, filename, 'application/json');
}

export function exportLogsToCSV(logs: any[], filename: string = 'logs.csv'): void {
  const columns = [
    'timestamp',
    'service_name',
    'level',
    'message',
    'pod_name',
    'namespace',
    'trace_id',
  ];

  const formattedLogs = logs.map(log => ({
    ...log,
    timestamp: formatTimestamp(log.timestamp),
    message: log.message?.substring(0, 500),
  }));

  exportToCSV(formattedLogs, columns, filename);
}

export function exportTopologyToJSON(topology: any, filename: string = 'topology.json'): void {
  exportToJSON(topology, filename);
}

export function exportAnalysisToJSON(analysis: any, filename: string = 'analysis.json'): void {
  exportToJSON(analysis, filename);
}

function downloadFile(content: string, filename: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function formatTimestamp(timestamp: string): string {
  try {
    const date = new Date(timestamp);
    return date.toISOString();
  } catch {
    return timestamp;
  }
}

export function generateExportFilename(prefix: string, format: string): string {
  const now = new Date();
  const dateStr = now.toISOString().split('T')[0];
  const timeStr = now.toTimeString().split(' ')[0].replace(/:/g, '-');
  return `${prefix}_${dateStr}_${timeStr}.${format}`;
}
