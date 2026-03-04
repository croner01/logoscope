import React, { useMemo, useState } from 'react';

type DiffLineType = 'context' | 'added' | 'removed';

interface DiffLine {
  type: DiffLineType;
  text: string;
  beforeLine?: number;
  afterLine?: number;
}

interface HistoryDiffViewProps {
  beforeValue: any;
  afterValue: any;
  maxHeightClassName?: string;
}

const formatDiffValue = (value: any): string => {
  if (value === null || value === undefined) {
    return '-';
  }
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

const toLines = (value: any): string[] =>
  formatDiffValue(value).replace(/\r\n/g, '\n').split('\n');

const buildLineDiff = (beforeValue: any, afterValue: any): DiffLine[] => {
  const beforeLines = toLines(beforeValue);
  const afterLines = toLines(afterValue);
  const n = beforeLines.length;
  const m = afterLines.length;

  // 超长内容时避免 O(n*m) 过高开销，退化为“前后块”差异展示。
  if (n * m > 250000) {
    const removed = beforeLines.slice(0, 200).map((text, index) => ({
      type: 'removed' as const,
      text,
      beforeLine: index + 1,
    }));
    const added = afterLines.slice(0, 200).map((text, index) => ({
      type: 'added' as const,
      text,
      afterLine: index + 1,
    }));
    return [...removed, ...added];
  }

  const dp: number[][] = Array.from({ length: n + 1 }, () => Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = beforeLines[i] === afterLines[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const output: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (beforeLines[i] === afterLines[j]) {
      output.push({
        type: 'context',
        text: beforeLines[i],
        beforeLine: i + 1,
        afterLine: j + 1,
      });
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      output.push({
        type: 'removed',
        text: beforeLines[i],
        beforeLine: i + 1,
      });
      i += 1;
    } else {
      output.push({
        type: 'added',
        text: afterLines[j],
        afterLine: j + 1,
      });
      j += 1;
    }
  }

  while (i < n) {
    output.push({
      type: 'removed',
      text: beforeLines[i],
      beforeLine: i + 1,
    });
    i += 1;
  }
  while (j < m) {
    output.push({
      type: 'added',
      text: afterLines[j],
      afterLine: j + 1,
    });
    j += 1;
  }
  return output;
};

const HistoryDiffView: React.FC<HistoryDiffViewProps> = ({
  beforeValue,
  afterValue,
  maxHeightClassName = 'max-h-56',
}) => {
  const [onlyChangedLines, setOnlyChangedLines] = useState(false);
  const diffLines = useMemo(() => buildLineDiff(beforeValue, afterValue), [beforeValue, afterValue]);
  const changedCount = diffLines.filter((line) => line.type !== 'context').length;
  const visibleLines = onlyChangedLines
    ? diffLines.filter((line) => line.type !== 'context')
    : diffLines;

  return (
    <div className="mt-1 rounded border border-slate-200 bg-white">
      <div className="px-2 py-1 border-b border-slate-100 text-[11px] text-slate-500 flex items-center justify-between gap-2">
        <span>行级差异（+ 新增 / - 删除），变更行 {changedCount}</span>
        <label className="inline-flex items-center gap-1.5 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={onlyChangedLines}
            onChange={(e) => setOnlyChangedLines(e.target.checked)}
            className="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
          />
          <span className="text-[11px] text-slate-600">仅看变更行（隐藏上下文）</span>
        </label>
      </div>
      <div className={`${maxHeightClassName} overflow-auto font-mono text-[11px] leading-5`}>
        {visibleLines.length === 0 ? (
          <div className="px-2 py-1 text-slate-500">无变更行</div>
        ) : visibleLines.map((line, index) => {
          const prefix = line.type === 'added' ? '+' : line.type === 'removed' ? '-' : ' ';
          const rowClass = line.type === 'added'
            ? 'bg-emerald-50 text-emerald-700'
            : line.type === 'removed'
              ? 'bg-rose-50 text-rose-700'
              : 'text-slate-500';
          const lineNo = line.type === 'added'
            ? `+${line.afterLine || ''}`
            : line.type === 'removed'
              ? `-${line.beforeLine || ''}`
              : String(line.beforeLine || '');
          return (
            <div key={`${line.type}-${line.beforeLine || 0}-${line.afterLine || 0}-${index}`} className={`flex px-2 py-0.5 ${rowClass}`}>
              <span className="w-12 shrink-0 text-[10px] text-slate-400 select-none">{lineNo}</span>
              <span className="whitespace-pre-wrap break-all">{`${prefix} ${line.text || ' '}`}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default HistoryDiffView;
