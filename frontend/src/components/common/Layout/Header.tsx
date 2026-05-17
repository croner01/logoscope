/**
 * Header — Logoscope Professional Topbar
 * Global search, breadcrumb, time-range, status, user menu
 */
import React, { useState, useEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { RefreshCw, Bell, Search, ChevronRight, Moon, Sun, Command } from 'lucide-react';
import TimeRangePicker from '../TimeRangePicker';

/* ─── Route label map ─────────────────────────────────────────────────────── */
const ROUTE_META: Record<string, { label: string; section: string }> = {
  '/dashboard':       { label: '仪表盘',        section: '监控' },
  '/alerts':          { label: '告警中心',       section: '监控' },
  '/logs':            { label: '日志浏览器',     section: '数据探索' },
  '/traces':          { label: '链路追踪',       section: '数据探索' },
  '/topology':        { label: '服务拓扑',       section: '数据探索' },
  '/labels':          { label: '标签发现',       section: '数据探索' },
  '/ai-analysis':     { label: 'AI 智能分析',    section: 'AI 智能' },
  '/ai-runtime-lab':  { label: 'AI Runtime Lab', section: 'AI 智能' },
  '/ai-cases':        { label: '知识库管理',     section: 'AI 智能' },
  '/settings':        { label: '设置',           section: '系统' },
};

/* ─── Quick-search suggestions ───────────────────────────────────────────── */
const QUICK_LINKS = [
  { label: '仪表盘', path: '/dashboard' },
  { label: '日志浏览器', path: '/logs' },
  { label: '链路追踪', path: '/traces' },
  { label: '告警中心', path: '/alerts' },
  { label: '服务拓扑', path: '/topology' },
  { label: 'AI 分析', path: '/ai-analysis' },
];

interface HeaderProps {
  onRefresh?: () => void;
}

const Header: React.FC<HeaderProps> = ({ onRefresh }) => {
  const location = useLocation();
  const navigate = useNavigate();
  const [timeRange, setTimeRange] = useState('1h');
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [darkMode, setDarkMode] = useState(false);
  const searchRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  /* breadcrumb */
  const routeKey = Object.keys(ROUTE_META)
    .find(k => location.pathname === k || location.pathname.startsWith(k + '/')) ?? '';
  const meta = ROUTE_META[routeKey];

  /* keyboard shortcut: ⌘K / Ctrl+K */
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setSearchOpen(prev => !prev);
      }
      if (e.key === 'Escape') setSearchOpen(false);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  /* focus input when search opens */
  useEffect(() => {
    if (searchOpen) setTimeout(() => searchInputRef.current?.focus(), 50);
  }, [searchOpen]);

  /* click-outside to close */
  useEffect(() => {
    if (!searchOpen) return;
    const handler = (e: MouseEvent) => {
      if (!searchRef.current?.contains(e.target as Node)) setSearchOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [searchOpen]);

  const filteredLinks = QUICK_LINKS.filter(l =>
    !searchQuery || l.label.includes(searchQuery)
  );

  return (
    <header
      className="flex-shrink-0 flex items-center justify-between px-5 z-30"
      style={{
        height: 'var(--header-height)',
        background: 'var(--header-bg)',
        borderBottom: '1px solid var(--header-border)',
        backdropFilter: 'blur(12px)',
        WebkitBackdropFilter: 'blur(12px)',
      }}
    >
      {/* ── Left: Breadcrumb ─────────────────────────────────────────────── */}
      <div className="flex items-center gap-1.5 min-w-0">
        {meta ? (
          <>
            <span className="text-xs font-medium" style={{ color: 'var(--app-text-subtle)' }}>
              {meta.section}
            </span>
            <ChevronRight size={12} style={{ color: 'var(--app-text-subtle)', flexShrink: 0 }} />
            <span className="text-sm font-semibold truncate" style={{ color: 'var(--app-text)' }}>
              {meta.label}
            </span>
          </>
        ) : (
          <span className="text-sm font-semibold" style={{ color: 'var(--app-text)' }}>
            Logoscope
          </span>
        )}
      </div>

      {/* ── Center: Global Search ────────────────────────────────────────── */}
      <div className="flex-1 max-w-sm mx-6 relative" ref={searchRef}>
        <button
          onClick={() => setSearchOpen(true)}
          className="w-full flex items-center gap-2.5 px-3 h-8 rounded-lg text-left transition-all duration-150"
          style={{
            background: 'var(--app-surface-muted)',
            border: '1px solid var(--app-border)',
            color: 'var(--app-text-subtle)',
          }}
        >
          <Search size={13} />
          <span className="text-xs flex-1">快速跳转…</span>
          <span
            className="hidden sm:flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium"
            style={{ background: 'var(--app-border)', color: 'var(--app-text-muted)' }}
          >
            <Command size={9} />K
          </span>
        </button>

        {/* Search dropdown */}
        {searchOpen && (
          <div
            className="absolute top-full left-0 right-0 mt-1.5 rounded-xl overflow-hidden z-50 animate-fade-in"
            style={{
              background: 'var(--app-surface)',
              border: '1px solid var(--app-border)',
              boxShadow: 'var(--shadow-lg)',
            }}
          >
            <div className="p-2 border-b" style={{ borderColor: 'var(--app-border-subtle)' }}>
              <div className="flex items-center gap-2 px-2">
                <Search size={13} style={{ color: 'var(--app-text-subtle)' }} />
                <input
                  ref={searchInputRef}
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                  placeholder="搜索页面…"
                  className="flex-1 text-sm bg-transparent outline-none"
                  style={{ color: 'var(--app-text)' }}
                />
              </div>
            </div>
            <div className="p-1.5">
              <div className="text-[10px] font-semibold uppercase tracking-wider px-2 py-1"
                style={{ color: 'var(--app-text-subtle)' }}>
                快速跳转
              </div>
              {filteredLinks.map(link => (
                <button
                  key={link.path}
                  onClick={() => { navigate(link.path); setSearchOpen(false); setSearchQuery(''); }}
                  className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-left text-sm transition-colors duration-100"
                  style={{ color: 'var(--app-text)' }}
                  onMouseEnter={e => (e.currentTarget.style.background = 'var(--app-surface-hover)')}
                  onMouseLeave={e => (e.currentTarget.style.background = '')}
                >
                  {link.label}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* ── Right: Actions ───────────────────────────────────────────────── */}
      <div className="flex items-center gap-1.5 flex-shrink-0">
        {/* Time Range */}
        <TimeRangePicker value={timeRange} onChange={setTimeRange} />

        {/* Divider */}
        <div className="w-px h-5 mx-1" style={{ background: 'var(--app-border)' }} />

        {/* Refresh */}
        <HeaderIconBtn title="刷新数据" onClick={onRefresh}>
          <RefreshCw size={15} />
        </HeaderIconBtn>

        {/* Dark mode toggle (cosmetic placeholder) */}
        <HeaderIconBtn title={darkMode ? '切换亮色' : '切换暗色'} onClick={() => setDarkMode(p => !p)}>
          {darkMode ? <Sun size={15} /> : <Moon size={15} />}
        </HeaderIconBtn>

        {/* Notifications */}
        <div className="relative">
          <HeaderIconBtn title="通知">
            <Bell size={15} />
          </HeaderIconBtn>
          <span
            className="absolute top-1 right-1 w-1.5 h-1.5 rounded-full"
            style={{ background: 'var(--color-error)' }}
          />
        </div>

        {/* Divider */}
        <div className="w-px h-5 mx-1" style={{ background: 'var(--app-border)' }} />

        {/* User avatar */}
        <button
          className="flex items-center gap-2 pl-1 pr-2 py-1 rounded-lg transition-colors duration-150 group"
          style={{ color: 'var(--app-text-muted)' }}
          onMouseEnter={e => (e.currentTarget.style.background = 'var(--app-surface-hover)')}
          onMouseLeave={e => (e.currentTarget.style.background = '')}
        >
          <div
            className="w-7 h-7 rounded-full flex items-center justify-center text-white text-xs font-bold"
            style={{
              background: 'linear-gradient(135deg, #10b981, #0d9488)',
              boxShadow: '0 1px 4px rgba(13,148,136,0.35)',
            }}
          >
            A
          </div>
          <span className="text-xs font-medium hidden md:block" style={{ color: 'var(--app-text)' }}>
            管理员
          </span>
        </button>
      </div>
    </header>
  );
};

/* ─── Helper ──────────────────────────────────────────────────────────────── */
function HeaderIconBtn({
  children,
  title,
  onClick,
}: {
  children: React.ReactNode;
  title?: string;
  onClick?: () => void;
}) {
  return (
    <button
      title={title}
      onClick={onClick}
      className="w-8 h-8 flex items-center justify-center rounded-lg transition-colors duration-150"
      style={{ color: 'var(--app-text-muted)' }}
      onMouseEnter={e => {
        (e.currentTarget as HTMLButtonElement).style.background = 'var(--app-surface-hover)';
        (e.currentTarget as HTMLButtonElement).style.color = 'var(--app-text)';
      }}
      onMouseLeave={e => {
        (e.currentTarget as HTMLButtonElement).style.background = '';
        (e.currentTarget as HTMLButtonElement).style.color = 'var(--app-text-muted)';
      }}
    >
      {children}
    </button>
  );
}

export default Header;
