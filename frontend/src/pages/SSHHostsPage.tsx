/**
 * SSH 主机管理页面
 * 支持注册、列表查看、删除 SSH 主机，支持粘贴私钥内容注册节点
 */
import React, { useCallback, useEffect, useState } from 'react';
import {
  CheckCircle2,
  Plus,
  RefreshCw,
  Server,
  Trash2,
  X,
} from 'lucide-react';

import ErrorState from '../components/common/ErrorState';
import LoadingState from '../components/common/LoadingState';
import { api } from '../utils/api';

/* ─── Types ──────────────────────────────────────────────────────────── */

interface HostRecord {
  name: string;
  host: string;
  port: number;
  user: string;
  key_file: string;
  labels: Record<string, string>;
  created_at: string;
  updated_at: string;
}

interface BannerMessage {
  type: 'success' | 'error' | 'info';
  text: string;
}

interface RegisterFormData {
  name: string;
  host: string;
  port: string;
  user: string;
  key_file: string;
  private_key: string;
  labels_key: string;
  labels_value: string;
}

const EMPTY_FORM: RegisterFormData = {
  name: '',
  host: '',
  port: '22',
  user: 'root',
  key_file: '/etc/ssh-keys/default/id_rsa',
  private_key: '',
  labels_key: '',
  labels_value: '',
};

/* ─── Helper ──────────────────────────────────────────────────────────── */

function getErrorMessage(error: unknown, fallback: string): string {
  const err = error as Record<string, unknown>;
  const resp = err?.response as Record<string, unknown> | undefined;
  const data = resp?.data as Record<string, unknown> | undefined;
  const detail = data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (typeof err?.message === 'string' && err.message.trim()) return err.message.trim();
  return fallback;
}

function formatTime(iso: string): string {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false });
  } catch {
    return iso;
  }
}

/* ─── Component ──────────────────────────────────────────────────────── */

const SSHHostsPage: React.FC = () => {
  const [hosts, setHosts] = useState<HostRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [banner, setBanner] = useState<BannerMessage | null>(null);

  // Register modal
  const [showRegister, setShowRegister] = useState(false);
  const [form, setForm] = useState<RegisterFormData>(EMPTY_FORM);
  const [registering, setRegistering] = useState(false);

  // Delete confirmation
  const [deleting, setDeleting] = useState<string | null>(null);

  /* ─── Data fetching ────────────────────────────────────────────────── */

  const fetchHosts = useCallback(async (options?: { initial?: boolean }) => {
    const initial = Boolean(options?.initial);
    if (initial) setLoading(true);
    else setRefreshing(true);
    setLoadError(null);

    try {
      const data = await api.listHosts();
      setHosts(Array.isArray(data) ? (data as unknown as HostRecord[]) : []);
    } catch (error) {
      console.error('Failed to fetch hosts:', error);
      if (initial) {
        setLoadError('SSH 主机列表加载失败，请检查 SSH Gateway 服务是否可用。');
      } else {
        setBanner({ type: 'error', text: getErrorMessage(error, '刷新主机列表失败') });
      }
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchHosts({ initial: true });
  }, [fetchHosts]);

  /* ─── Register host ────────────────────────────────────────────────── */

  const handleRegister = async () => {
    const name = form.name.trim();
    const host = form.host.trim();
    if (!name || !host) {
      setBanner({ type: 'error', text: '主机名和 IP/域名不能为空' });
      return;
    }

    const labels: Record<string, string> = {};
    const lk = form.labels_key.trim();
    const lv = form.labels_value.trim();
    if (lk && lv) labels[lk] = lv;

    setRegistering(true);
    try {
      const kf = form.key_file.trim();
      await api.registerHost({
        name,
        host,
        port: parseInt(form.port, 10) || 22,
        user: form.user.trim() || 'root',
        // Only send key_file when explicitly provided (private_key takes priority)
        key_file: kf || undefined,
        private_key: form.private_key.trim() || undefined,
        labels: Object.keys(labels).length > 0 ? labels : undefined,
      });
      setBanner({ type: 'success', text: `主机 "${name}" 注册成功` });
      setShowRegister(false);
      setForm(EMPTY_FORM);
      await fetchHosts();
    } catch (error) {
      console.error('Failed to register host:', error);
      setBanner({ type: 'error', text: getErrorMessage(error, '注册主机失败') });
    } finally {
      setRegistering(false);
    }
  };

  /* ─── Delete host ──────────────────────────────────────────────────── */

  const handleDelete = async (name: string) => {
    setDeleting(name);
    try {
      await api.deleteHost(name);
      setBanner({ type: 'success', text: `主机 "${name}" 已删除` });
      setHosts((prev) => prev.filter((h) => h.name !== name));
    } catch (error) {
      console.error('Failed to delete host:', error);
      setBanner({ type: 'error', text: getErrorMessage(error, `删除主机 "${name}" 失败`) });
    } finally {
      setDeleting(null);
    }
  };

  const confirmDelete = (name: string) => {
    if (window.confirm(`确定要删除主机 "${name}" 吗？此操作不可撤销。`)) {
      handleDelete(name);
    }
  };

  /* ─── Reset banner ─────────────────────────────────────────────────── */

  const clearBanner = () => setBanner(null);

  /* ─── Render ───────────────────────────────────────────────────────── */

  if (loading) return <LoadingState message="加载 SSH 主机列表..." />;

  if (loadError) return <ErrorState message={loadError} onRetry={() => fetchHosts({ initial: true })} />;

  return (
    <div className="flex flex-col h-full">
      {/* ── Banner ─────────────────────────────────────────────────── */}
      {banner && (
        <div
          className="flex-shrink-0 mx-6 mt-4 px-4 py-3 rounded-xl text-sm flex items-center justify-between gap-2 animate-fade-in"
          style={{
            background: banner.type === 'success' ? 'var(--color-success-soft)' : banner.type === 'error' ? 'var(--color-error-soft)' : 'var(--color-info-soft)',
            border: `1px solid ${banner.type === 'success' ? '#a7f3d0' : banner.type === 'error' ? '#fca5a5' : '#bfdbfe'}`,
            color: banner.type === 'success' ? 'var(--color-success-dark)' : banner.type === 'error' ? 'var(--color-error-dark)' : 'var(--color-info-dark)',
          }}
        >
          <div className="flex items-center gap-2">
            <CheckCircle2 size={14} />
            {banner.text}
          </div>
          <button onClick={clearBanner} className="opacity-60 hover:opacity-100">
            <X size={14} />
          </button>
        </div>
      )}

      {/* ── Page header ────────────────────────────────────────────── */}
      <div className="flex-shrink-0 px-6 py-4 border-b" style={{ background: 'var(--app-surface)', borderColor: 'var(--app-border)' }}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center" style={{ background: 'var(--color-success-soft)', color: 'var(--color-success-dark)' }}>
              <Server size={18} />
            </div>
            <div>
              <h1 className="text-base font-bold" style={{ color: 'var(--app-text)' }}>SSH 主机管理</h1>
              <p className="text-xs" style={{ color: 'var(--app-text-subtle)' }}>
                管理远程主机注册，支持粘贴 SSH 私钥注册节点
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => fetchHosts()}
              disabled={refreshing}
              className="btn btn-secondary"
            >
              <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} />
              刷新
            </button>
            <button
              onClick={() => { setShowRegister(true); setBanner(null); }}
              className="btn btn-primary"
            >
              <Plus size={13} />
              注册主机
            </button>
          </div>
        </div>
      </div>

      {/* ── Content ────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-auto p-6">
        {hosts.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 gap-4">
            <div className="w-16 h-16 rounded-2xl flex items-center justify-center" style={{ background: 'var(--app-surface-muted)' }}>
              <Server size={32} style={{ color: 'var(--app-text-subtle)' }} />
            </div>
            <div className="text-sm font-medium" style={{ color: 'var(--app-text-subtle)' }}>
              暂无注册的主机
            </div>
            <p className="text-xs" style={{ color: 'var(--app-text-subtle)' }}>
              点击"注册主机"按钮添加第一台远程主机
            </p>
            <button
              onClick={() => { setShowRegister(true); setBanner(null); }}
              className="btn btn-primary mt-2"
            >
              <Plus size={13} />
              注册主机
            </button>
          </div>
        ) : (
          <div className="card overflow-hidden">
            <div className="card-header">
              <div className="card-title">
                <Server size={14} style={{ color: 'var(--color-success)' }} />
                已注册主机
                <span className="badge badge-neutral ml-1">{hosts.length}</span>
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--app-border)', background: 'var(--app-surface-muted)' }}>
                    <th className="text-left py-3 px-4 font-semibold" style={{ color: 'var(--app-text-muted)' }}>名称</th>
                    <th className="text-left py-3 px-4 font-semibold" style={{ color: 'var(--app-text-muted)' }}>主机</th>
                    <th className="text-left py-3 px-4 font-semibold" style={{ color: 'var(--app-text-muted)' }}>端口</th>
                    <th className="text-left py-3 px-4 font-semibold" style={{ color: 'var(--app-text-muted)' }}>用户</th>
                    <th className="text-left py-3 px-4 font-semibold" style={{ color: 'var(--app-text-muted)' }}>密钥文件</th>
                    <th className="text-left py-3 px-4 font-semibold" style={{ color: 'var(--app-text-muted)' }}>标签</th>
                    <th className="text-left py-3 px-4 font-semibold" style={{ color: 'var(--app-text-muted)' }}>创建时间</th>
                    <th className="text-right py-3 px-4 font-semibold" style={{ color: 'var(--app-text-muted)' }}>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {hosts.map((record) => (
                    <tr
                      key={record.name}
                      style={{ borderBottom: '1px solid var(--app-border-subtle)' }}
                      className="hover:bg-white/5 transition-colors"
                    >
                      <td className="py-3 px-4 font-medium" style={{ color: 'var(--app-text)' }}>{record.name}</td>
                      <td className="py-3 px-4" style={{ color: 'var(--brand-primary)' }}>{record.host}</td>
                      <td className="py-3 px-4" style={{ color: 'var(--app-text)' }}>{record.port}</td>
                      <td className="py-3 px-4" style={{ color: 'var(--app-text)' }}>{record.user}</td>
                      <td className="py-3 px-4 max-w-[200px] truncate" style={{ color: 'var(--app-text-muted)' }} title={record.key_file !== '-' ? record.key_file : ''}>
                        <code className="text-xs">{record.key_file === '-' ? <span style={{ color: 'var(--app-text-subtle)' }}>-</span> : record.key_file}</code>
                      </td>
                      <td className="py-3 px-4">
                        <div className="flex flex-wrap gap-1">
                          {Object.entries(record.labels || {}).length > 0 ? (
                            Object.entries(record.labels).map(([k, v]) => (
                              <span key={k} className="badge badge-neutral text-[10px]">{k}={v}</span>
                            ))
                          ) : (
                            <span style={{ color: 'var(--app-text-subtle)' }}>-</span>
                          )}
                        </div>
                      </td>
                      <td className="py-3 px-4 whitespace-nowrap" style={{ color: 'var(--app-text-subtle)' }}>
                        {formatTime(record.created_at)}
                      </td>
                      <td className="py-3 px-4 text-right">
                        <button
                          onClick={() => confirmDelete(record.name)}
                          disabled={deleting === record.name}
                          className="btn btn-danger btn-sm"
                          title="删除主机"
                        >
                          {deleting === record.name ? <RefreshCw size={11} className="animate-spin" /> : <Trash2 size={11} />}
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {/* ── Register Modal ─────────────────────────────────────────── */}
      {showRegister && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
          <div
            className="w-full max-w-lg mx-4 rounded-xl shadow-2xl border overflow-hidden"
            style={{ background: 'var(--app-surface)', borderColor: 'var(--app-border)' }}
          >
            {/* Modal header */}
            <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor: 'var(--app-border)' }}>
              <div className="flex items-center gap-2">
                <Server size={16} style={{ color: 'var(--color-success)' }} />
                <span className="text-sm font-bold" style={{ color: 'var(--app-text)' }}>注册新主机</span>
              </div>
              <button onClick={() => setShowRegister(false)} style={{ color: 'var(--app-text-subtle)' }} className="hover:opacity-70">
                <X size={16} />
              </button>
            </div>

            {/* Modal body */}
            <div className="px-5 py-4 space-y-3 max-h-[60vh] overflow-y-auto">
              <div className="grid grid-cols-2 gap-3">
                <div className="col-span-1">
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>主机名 *</label>
                  <input
                    type="text"
                    value={form.name}
                    onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
                    placeholder="例如: node-3"
                    className="input"
                  />
                </div>
                <div className="col-span-1">
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>IP / 域名 *</label>
                  <input
                    type="text"
                    value={form.host}
                    onChange={(e) => setForm((p) => ({ ...p, host: e.target.value }))}
                    placeholder="例如: 10.0.0.1"
                    className="input"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>SSH 端口</label>
                  <input
                    type="number"
                    min={1}
                    max={65535}
                    value={form.port}
                    onChange={(e) => setForm((p) => ({ ...p, port: e.target.value }))}
                    className="input"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>SSH 用户</label>
                  <input
                    type="text"
                    value={form.user}
                    onChange={(e) => setForm((p) => ({ ...p, user: e.target.value }))}
                    className="input"
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>密钥文件路径</label>
                <input
                  type="text"
                  value={form.key_file}
                  onChange={(e) => setForm((p) => ({ ...p, key_file: e.target.value }))}
                  className="input"
                />
                <p className="text-[10px] mt-1" style={{ color: 'var(--app-text-subtle)' }}>
                  与私钥内容二选一。填写路径时需密钥文件已在 Pod 上存在（Secret 挂载方式）
                </p>
              </div>

              <div>
                <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>
                  SSH 私钥内容
                  <span className="ml-1 font-normal" style={{ color: 'var(--app-text-subtle)' }}>(粘贴私钥文件内容，Base64 编码存储)</span>
                </label>
                <textarea
                  value={form.private_key}
                  onChange={(e) => {
                    const val = e.target.value;
                    setForm((p) => ({
                      ...p,
                      private_key: val,
                      // 填入私钥内容时自动清空密钥文件路径（二选一）
                      key_file: val.trim() ? '' : p.key_file,
                    }));
                  }}
                  rows={6}
                  className="input font-mono text-xs"
                  placeholder={`-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----`}
                />
                <p className="text-[10px] mt-1" style={{ color: 'var(--app-text-subtle)' }}>
                  粘贴私钥内容后，SSH Gateway 将写入临时文件建立连接，无需提供密钥文件路径
                </p>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>标签 Key</label>
                  <input
                    type="text"
                    value={form.labels_key}
                    onChange={(e) => setForm((p) => ({ ...p, labels_key: e.target.value }))}
                    placeholder="例如: env"
                    className="input"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>标签 Value</label>
                  <input
                    type="text"
                    value={form.labels_value}
                    onChange={(e) => setForm((p) => ({ ...p, labels_value: e.target.value }))}
                    placeholder="例如: prod"
                    className="input"
                  />
                </div>
              </div>
            </div>

            {/* Modal footer */}
            <div className="flex items-center justify-end gap-2 px-5 py-4 border-t" style={{ borderColor: 'var(--app-border)' }}>
              <button
                onClick={() => setShowRegister(false)}
                className="btn btn-secondary"
                disabled={registering}
              >
                取消
              </button>
              <button
                onClick={handleRegister}
                disabled={registering}
                className="btn btn-primary"
              >
                {registering ? <RefreshCw size={12} className="animate-spin" /> : <Plus size={12} />}
                注册
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default SSHHostsPage;
