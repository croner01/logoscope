import React, { useCallback, useRef, useState } from 'react';
import { Upload, X, FileText, FileJson, AlertCircle, CheckCircle2, Loader2 } from 'lucide-react';
import { api, type UploadResult } from '../../utils/api';

interface UploadDialogProps {
  open: boolean;
  onClose: () => void;
  onSuccess?: (result: UploadResult) => void;
}

type UploadState = 'idle' | 'uploading' | 'success' | 'error';

const UploadDialog: React.FC<UploadDialogProps> = ({ open, onClose, onSuccess }) => {
  const [file, setFile] = useState<File | null>(null);
  const [serviceName, setServiceName] = useState('');
  const [namespace, setNamespace] = useState('default');
  const [uploadState, setUploadState] = useState<UploadState>('idle');
  const [progress, setProgress] = useState(0);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (f) setFile(f);
  }, []);

  const handleSelectFile = () => inputRef.current?.click();

  const handleUpload = async () => {
    if (!file) return;
    setUploadState('uploading');
    setProgress(0);
    setError('');
    abortRef.current = new AbortController();

    try {
      const res = await api.uploadLogs(file, {
        serviceName: serviceName || undefined,
        namespace: namespace || undefined,
        onProgress: setProgress,
        signal: abortRef.current.signal,
      });
      setResult(res);
      setUploadState('success');
      onSuccess?.(res);
    } catch (err: unknown) {
      if ((err as { name?: string })?.name === 'CanceledError' || (err as { name?: string })?.name === 'AbortError') {
        setUploadState('idle');
        return;
      }
      setError((err as { message?: string })?.message || 'Upload failed');
      setUploadState('error');
    }
  };

  const handleCancel = () => {
    abortRef.current?.abort();
    setUploadState('idle');
  };

  const handleReset = () => {
    setFile(null);
    setUploadState('idle');
    setProgress(0);
    setResult(null);
    setError('');
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div className="w-full max-w-lg rounded-xl bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-slate-800">上传日志文件</h2>
          <button onClick={onClose} className="btn btn-ghost btn-icon"><X className="h-5 w-5" /></button>
        </div>

        {/* Drop zone */}
        {!file && uploadState === 'idle' && (
          <div
            className="mb-4 flex cursor-pointer flex-col items-center rounded-lg border-2 border-dashed border-slate-300 p-8 text-slate-500 transition-colors hover:border-blue-400 hover:bg-blue-50"
            onDrop={handleDrop}
            onDragOver={(e) => e.preventDefault()}
            onClick={handleSelectFile}
          >
            <Upload className="mb-2 h-8 w-8" />
            <p className="text-sm font-medium">拖拽文件到此处，或点击选择文件</p>
            <p className="mt-1 text-xs text-slate-400">支持 .log .txt .json .ndjson，最大 500MB</p>
            <input
              ref={inputRef}
              type="file"
              accept=".log,.txt,.json,.ndjson"
              className="hidden"
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
          </div>
        )}

        {/* Selected file info */}
        {file && uploadState === 'idle' && (
          <div className="mb-4 flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
            {file.name.endsWith('.json') || file.name.endsWith('.ndjson')
              ? <FileJson className="h-6 w-6 text-blue-500" />
              : <FileText className="h-6 w-6 text-slate-500" />
            }
            <div className="flex-1 min-w-0">
              <p className="truncate text-sm font-medium text-slate-700">{file.name}</p>
              <p className="text-xs text-slate-400">{formatFileSize(file.size)}</p>
            </div>
            <button onClick={handleReset} className="btn btn-ghost btn-icon"><X className="h-4 w-4" /></button>
          </div>
        )}

        {/* Options */}
        {uploadState === 'idle' && (
          <div className="mb-4 space-y-2">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">服务名（可选，留空自动识别）</label>
              <input
                value={serviceName}
                onChange={(e) => setServiceName(e.target.value)}
                placeholder="自动识别"
                className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-blue-400"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">命名空间</label>
              <input
                value={namespace}
                onChange={(e) => setNamespace(e.target.value)}
                className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-blue-400"
              />
            </div>
          </div>
        )}

        {/* Progress */}
        {uploadState === 'uploading' && (
          <div className="mb-4">
            <div className="flex items-center gap-2 text-sm text-slate-600 mb-2">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span>正在上传... {progress}%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-slate-200">
              <div
                className="h-full rounded-full bg-blue-500 transition-all duration-300"
                style={{ width: `${progress}%` }}
              />
            </div>
          </div>
        )}

        {/* Success */}
        {uploadState === 'success' && result && (
          <div className="mb-4 flex items-start gap-3 rounded-lg border border-green-200 bg-green-50 p-3">
            <CheckCircle2 className="mt-0.5 h-5 w-5 text-green-600" />
            <div className="text-sm text-green-800">
              <p className="font-medium">上传成功</p>
              <p className="mt-0.5">已接收 {result.total} 条日志，分 {result.batches} 批写入管道</p>
              <p className="text-xs text-green-600 mt-0.5">日志将在数秒后出现在查询结果中</p>
            </div>
          </div>
        )}

        {/* Error */}
        {uploadState === 'error' && (
          <div className="mb-4 flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 p-3">
            <AlertCircle className="mt-0.5 h-5 w-5 text-red-600" />
            <div className="text-sm text-red-800">
              <p className="font-medium">上传失败</p>
              <p className="mt-0.5">{error}</p>
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="flex justify-end gap-2">
          {uploadState === 'idle' && (
            <>
              <button onClick={onClose} className="btn btn-ghost px-4 py-2 text-sm">取消</button>
              <button
                onClick={handleUpload}
                disabled={!file}
                className="btn btn-primary flex items-center gap-1.5 px-4 py-2 text-sm disabled:opacity-50"
              >
                <Upload className="h-4 w-4" />
                上传
              </button>
            </>
          )}
          {uploadState === 'uploading' && (
            <button onClick={handleCancel} className="btn btn-ghost px-4 py-2 text-sm">取消上传</button>
          )}
          {(uploadState === 'success' || uploadState === 'error') && (
            <>
              {uploadState === 'error' && (
                <button onClick={handleReset} className="btn btn-ghost px-4 py-2 text-sm">重试</button>
              )}
              <button onClick={onClose} className="btn btn-primary px-4 py-2 text-sm">关闭</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
};

export default UploadDialog;
