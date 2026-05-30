/**
 * Sidebar — Logoscope Professional Navigation
 * Grouped nav, icon-only collapse mode, brand identity
 */
import React, { useState, useCallback } from 'react';
import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  FileText,
  GitBranch,
  Network,
  Bell,
  BrainCircuit,
  BookMarked,
  Tags,
  Settings,
  Activity,
  FlaskConical,
  ChevronLeft,
  ChevronRight,
  User,
  Zap,
} from 'lucide-react';

/* ─── Nav structure ──────────────────────────────────────────────────────── */
interface NavItem {
  path: string;
  label: string;
  icon: React.ReactNode;
  badge?: number;
  shortcut?: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const navGroups: NavGroup[] = [
  {
    label: '监控',
    items: [
      { path: '/dashboard', label: '仪表盘',    icon: <LayoutDashboard size={16} />, shortcut: 'D' },
      { path: '/alerts',    label: '告警中心',  icon: <Bell size={16} />,            badge: 0 },
    ],
  },
  {
    label: '数据探索',
    items: [
      { path: '/logs',     label: '日志浏览器', icon: <FileText size={16} />,  shortcut: 'L' },
      { path: '/traces',   label: '链路追踪',   icon: <GitBranch size={16} />, shortcut: 'T' },
      { path: '/topology', label: '服务拓扑',   icon: <Network size={16} />,   shortcut: 'N' },
      { path: '/labels',   label: '标签发现',   icon: <Tags size={16} /> },
    ],
  },
  {
    label: 'AI 智能',
    items: [
      { path: '/ai-analysis',    label: 'AI 智能分析',    icon: <BrainCircuit size={16} /> },
      { path: '/ai-runtime-lab', label: 'AI Runtime Lab', icon: <FlaskConical size={16} /> },
      { path: '/ai-cases',       label: '知识库管理',     icon: <BookMarked size={16} /> },
    ],
  },
  {
    label: '系统',
    items: [
      { path: '/settings', label: '设置', icon: <Settings size={16} /> },
    ],
  },
];

/* ─── Props ──────────────────────────────────────────────────────────────── */
interface SidebarProps {
  collapsed?: boolean;
  onCollapseChange?: (collapsed: boolean) => void;
}

/* ─── Component ──────────────────────────────────────────────────────────── */
const Sidebar: React.FC<SidebarProps> = ({ collapsed: ctrl, onCollapseChange }) => {
  const [internal, setInternal] = useState(false);

  const collapsed = ctrl !== undefined ? ctrl : internal;

  const toggle = useCallback(() => {
    const next = !collapsed;
    if (ctrl === undefined) setInternal(next);
    onCollapseChange?.(next);
  }, [collapsed, ctrl, onCollapseChange]);

  return (
    <aside
      className={`
        relative flex flex-col h-screen overflow-hidden
        transition-all duration-300 ease-in-out
        ${collapsed ? 'w-[60px]' : 'w-[220px]'}
      `}
      style={{ background: 'var(--sidebar-bg)', borderRight: '1px solid var(--sidebar-border)' }}
    >
      {/* ── Subtle gradient overlay */}
      <div
        className="pointer-events-none absolute inset-0 z-0"
        style={{
          background: 'linear-gradient(180deg, rgba(13,148,136,0.08) 0%, transparent 40%)',
        }}
      />

      {/* ── Brand / Logo ─────────────────────────────────────────────────── */}
      <div
        className={`relative z-10 flex items-center h-14 border-b flex-shrink-0 transition-all duration-300 ${
          collapsed ? 'justify-center px-0' : 'px-4 gap-3'
        }`}
        style={{ borderColor: 'var(--sidebar-border)' }}
      >
        {/* Logo mark */}
        <div className="relative flex-shrink-0">
          <div
            className="w-8 h-8 rounded-[10px] flex items-center justify-center"
            style={{
              background: 'linear-gradient(135deg, #0d9488 0%, #0f766e 60%, #134e4a 100%)',
              boxShadow: '0 2px 8px rgba(13,148,136,0.45)',
            }}
          >
            <Activity size={16} className="text-white" strokeWidth={2.5} />
          </div>
          {/* Live dot */}
          <span
            className="absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full border border-[var(--sidebar-bg)] animate-status"
            style={{ background: 'var(--color-success)' }}
          />
        </div>

        {!collapsed && (
          <div className="min-w-0">
            <div
              className="text-sm font-bold tracking-tight leading-none"
              style={{
                background: 'linear-gradient(90deg, #f8fafc 0%, #99f6e4 100%)',
                WebkitBackgroundClip: 'text',
                WebkitTextFillColor: 'transparent',
              }}
            >
              Logoscope
            </div>
            <div className="text-[10px] mt-0.5" style={{ color: 'var(--sidebar-muted)' }}>
              Observability Platform
            </div>
          </div>
        )}
      </div>

      {/* ── Navigation ───────────────────────────────────────────────────── */}
      <nav className="relative z-10 flex-1 overflow-y-auto sidebar-scroll py-3">
        <div className={`space-y-5 ${collapsed ? 'px-2' : 'px-3'}`}>
          {navGroups.map((group) => (
            <div key={group.label}>
              {/* Group label */}
              {!collapsed && (
                <div
                  className="mb-1.5 px-2 text-[10px] font-semibold uppercase tracking-widest"
                  style={{ color: 'var(--sidebar-muted)' }}
                >
                  {group.label}
                </div>
              )}

              <ul className="space-y-0.5">
                {group.items.map((item) => (
                  <li key={item.path}>
                    <NavLink
                      to={item.path}
                      className={({ isActive }) =>
                        `group relative flex items-center rounded-[8px] transition-all duration-150
                        ${collapsed ? 'justify-center w-9 h-9 mx-auto' : 'gap-2.5 px-2.5 py-2'}
                        ${
                          isActive
                            ? 'text-[var(--sidebar-active-text)]'
                            : 'text-[var(--sidebar-muted)] hover:text-[var(--sidebar-text)]'
                        }`
                      }
                      style={({ isActive }) => ({
                        background: isActive
                          ? 'var(--sidebar-active-bg)'
                          : undefined,
                      })}
                    >
                      {({ isActive }) => (
                        <>
                          {/* Active left bar */}
                          {isActive && !collapsed && (
                            <span
                              className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 rounded-full"
                              style={{ background: 'var(--sidebar-active-bar)' }}
                            />
                          )}

                          {/* Icon */}
                          <span
                            className={`flex-shrink-0 transition-colors ${
                              isActive
                                ? 'text-[var(--sidebar-active-text)]'
                                : 'text-[var(--sidebar-muted)] group-hover:text-[var(--sidebar-text)]'
                            }`}
                          >
                            {item.icon}
                          </span>

                          {/* Label */}
                          {!collapsed && (
                            <span className="text-[13px] font-medium leading-none truncate">
                              {item.label}
                            </span>
                          )}

                          {/* Badge */}
                          {!collapsed && item.badge !== undefined && item.badge > 0 && (
                            <span className="ml-auto text-[10px] font-bold px-1.5 py-0.5 rounded-full bg-red-500 text-white min-w-[18px] text-center leading-none">
                              {item.badge}
                            </span>
                          )}

                          {/* Collapsed tooltip */}
                          {collapsed && (
                            <div
                              className="
                                pointer-events-none absolute left-full ml-2.5 z-50
                                px-2.5 py-1.5 rounded-md text-xs font-medium whitespace-nowrap
                                opacity-0 invisible translate-x-1
                                group-hover:opacity-100 group-hover:visible group-hover:translate-x-0
                                transition-all duration-150
                              "
                              style={{
                                background: '#1e293b',
                                color: '#f1f5f9',
                                boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
                                border: '1px solid rgba(255,255,255,0.08)',
                              }}
                            >
                              {item.label}
                              {item.shortcut && (
                                <span className="ml-2 opacity-50 text-[10px]">⌘{item.shortcut}</span>
                              )}
                            </div>
                          )}
                        </>
                      )}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </nav>

      {/* ── Footer ───────────────────────────────────────────────────────── */}
      <div
        className="relative z-10 flex-shrink-0 border-t pt-2 pb-3 space-y-1"
        style={{ borderColor: 'var(--sidebar-border)', padding: collapsed ? '8px 8px 12px' : '8px 12px 12px' }}
      >
        {/* User row */}
        <button
          className={`w-full group flex items-center rounded-[8px] transition-all duration-150 hover:bg-white/5 ${
            collapsed ? 'justify-center py-2' : 'gap-2.5 px-2 py-2'
          }`}
        >
          <div
            className="w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0"
            style={{
              background: 'linear-gradient(135deg, #10b981, #0d9488)',
              boxShadow: '0 1px 4px rgba(13,148,136,0.4)',
            }}
          >
            <User size={13} className="text-white" />
          </div>
          {!collapsed && (
            <div className="text-left min-w-0">
              <div className="text-[12px] font-semibold truncate" style={{ color: 'var(--sidebar-text)' }}>
                管理员
              </div>
              <div className="text-[10px] truncate" style={{ color: 'var(--sidebar-muted)' }}>
                admin@logoscope.io
              </div>
            </div>
          )}
        </button>

        {/* System status + version (expanded only) */}
        {!collapsed && (
          <div
            className="flex items-center justify-between px-2 py-1.5 rounded-[8px]"
            style={{ background: 'rgba(16,185,129,0.07)', border: '1px solid rgba(16,185,129,0.12)' }}
          >
            <div className="flex items-center gap-1.5">
              <Zap size={10} style={{ color: 'var(--color-success)' }} />
              <span className="text-[10px] font-medium" style={{ color: '#6ee7b7' }}>
                系统运行正常
              </span>
            </div>
            <span className="text-[10px]" style={{ color: 'var(--sidebar-muted)' }}>v2.0</span>
          </div>
        )}

        {/* Toggle collapse */}
        <button
          onClick={toggle}
          title={collapsed ? '展开侧边栏' : '收起侧边栏'}
          className={`w-full flex items-center rounded-[8px] transition-all duration-150
            text-[var(--sidebar-muted)] hover:text-[var(--sidebar-text)] hover:bg-white/5
            ${collapsed ? 'justify-center py-2' : 'gap-2 px-2 py-2'}
          `}
        >
          {collapsed
            ? <ChevronRight size={15} />
            : (
              <>
                <ChevronLeft size={15} />
                <span className="text-[12px]">收起侧边栏</span>
              </>
            )
          }
        </button>
      </div>
    </aside>
  );
};

export default Sidebar;
