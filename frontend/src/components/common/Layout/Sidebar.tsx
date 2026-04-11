/**
 * 侧边栏导航组件 - 优化版
 * 支持收起/展开功能，参考 Datadog 设计风格
 */
import React, { useState, useCallback } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
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
} from 'lucide-react';

interface NavItem {
  path: string;
  label: string;
  icon: React.ReactNode;
  badge?: number;
  shortcut?: string;
}

const navItems: NavItem[] = [
  { path: '/dashboard', label: '仪表盘', icon: <LayoutDashboard className="w-5 h-5" />, shortcut: 'D' },
  { path: '/logs', label: '日志', icon: <FileText className="w-5 h-5" />, shortcut: 'L' },
  { path: '/traces', label: '追踪', icon: <GitBranch className="w-5 h-5" />, shortcut: 'T' },
  { path: '/topology', label: '拓扑', icon: <Network className="w-5 h-5" />, shortcut: 'N' },
  { path: '/alerts', label: '告警', icon: <Bell className="w-5 h-5" />, badge: 0 },
  { path: '/ai-analysis', label: 'AI 分析', icon: <BrainCircuit className="w-5 h-5" /> },
  { path: '/ai-runtime-lab', label: 'AI Runtime Lab', icon: <FlaskConical className="w-5 h-5" /> },
  { path: '/ai-cases', label: '知识库管理', icon: <BookMarked className="w-5 h-5" /> },
  { path: '/labels', label: '标签', icon: <Tags className="w-5 h-5" /> },
  { path: '/settings', label: '设置', icon: <Settings className="w-5 h-5" /> },
];

interface SidebarProps {
  collapsed?: boolean;
  onCollapseChange?: (collapsed: boolean) => void;
}

const Sidebar: React.FC<SidebarProps> = ({ collapsed: controlledCollapsed, onCollapseChange }) => {
  const [internalCollapsed, setInternalCollapsed] = useState(false);
  const location = useLocation();
  
  const collapsed = controlledCollapsed !== undefined ? controlledCollapsed : internalCollapsed;
  
  const toggleCollapse = useCallback(() => {
    const newCollapsed = !collapsed;
    if (controlledCollapsed === undefined) {
      setInternalCollapsed(newCollapsed);
    }
    onCollapseChange?.(newCollapsed);
  }, [collapsed, controlledCollapsed, onCollapseChange]);

  return (
    <aside 
      className={`bg-[var(--app-sidebar-bg)] text-[var(--app-sidebar-text)] flex flex-col transition-all duration-300 ease-in-out border-r border-[var(--app-sidebar-border)] ${
        collapsed ? 'w-16' : 'w-64'
      }`}
    >
      {/* Logo 区域 */}
      <div className={`h-14 flex items-center border-b border-[var(--app-sidebar-border)] transition-all duration-300 ${
        collapsed ? 'justify-center px-2' : 'px-4'
      }`}>
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-cyan-500 to-teal-600 flex items-center justify-center shrink-0">
            <Activity className="w-5 h-5 text-white" />
          </div>
          {!collapsed && (
            <span className="text-lg font-bold bg-gradient-to-r from-white to-cyan-200 bg-clip-text text-transparent">
              Logoscope
            </span>
          )}
        </div>
      </div>

      {/* 导航菜单 */}
      <nav className="flex-1 py-3 overflow-y-auto">
        <ul className="space-y-0.5 px-2">
          {navItems.map((item) => {
            const isActive = location.pathname === item.path || location.pathname.startsWith(`${item.path}/`);
            
            return (
              <li key={item.path}>
                <NavLink
                  to={item.path}
                  className={({ isActive }) =>
                    `flex items-center py-2.5 rounded-lg transition-all duration-200 group relative ${
                      collapsed ? 'justify-center px-2' : 'px-3'
                    } ${
                      isActive
                        ? 'bg-[var(--app-sidebar-active)] text-white shadow-lg shadow-cyan-900/20'
                        : 'text-[var(--app-sidebar-muted)] hover:bg-slate-800 hover:text-white'
                    }`
                  }
                >
                  <span className={`shrink-0 ${isActive ? 'text-white' : 'text-[var(--app-sidebar-muted)] group-hover:text-white'}`}>
                    {item.icon}
                  </span>
                  
                  {!collapsed && (
                    <>
                      <span className="ml-3 font-medium text-sm truncate">{item.label}</span>
                      {item.badge !== undefined && item.badge > 0 && (
                        <span className="ml-auto bg-red-500 text-white text-[10px] px-1.5 py-0.5 rounded-full min-w-[18px] text-center">
                          {item.badge}
                        </span>
                      )}
                    </>
                  )}
                  
                  {/* Tooltip for collapsed state */}
                  {collapsed && (
                    <div className="absolute left-full ml-2 px-2 py-1 bg-gray-900 text-white text-xs rounded opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all duration-200 whitespace-nowrap z-50 shadow-lg">
                      {item.label}
                      {item.shortcut && (
                        <span className="ml-2 text-gray-400">⌘{item.shortcut}</span>
                      )}
                    </div>
                  )}
                </NavLink>
              </li>
            );
          })}
        </ul>
      </nav>

      {/* 底部区域 */}
      <div className="border-t border-[var(--app-sidebar-border)] p-2 space-y-1">
        {/* 用户菜单 */}
        <button className={`w-full flex items-center py-2 rounded-lg transition-all duration-200 text-[var(--app-sidebar-muted)] hover:bg-slate-800 hover:text-white ${
          collapsed ? 'justify-center px-2' : 'px-3'
        }`}>
          <div className="w-7 h-7 rounded-full bg-gradient-to-br from-emerald-400 to-cyan-500 flex items-center justify-center shrink-0">
            <User className="w-4 h-4 text-white" />
          </div>
          {!collapsed && (
            <div className="ml-3 text-left overflow-hidden">
              <div className="text-sm font-medium text-white truncate">管理员</div>
              <div className="text-xs text-gray-500 truncate">admin@logoscope.io</div>
            </div>
          )}
        </button>

        {/* 收起/展开按钮 */}
        <button
          onClick={toggleCollapse}
          className={`w-full flex items-center py-2 rounded-lg transition-all duration-200 text-[var(--app-sidebar-muted)] hover:bg-slate-800 hover:text-white ${
            collapsed ? 'justify-center px-2' : 'px-3'
          }`}
          title={collapsed ? '展开侧边栏' : '收起侧边栏'}
        >
          {collapsed ? (
            <ChevronRight className="w-5 h-5" />
          ) : (
            <>
              <ChevronLeft className="w-5 h-5" />
              <span className="ml-3 text-sm">收起侧边栏</span>
            </>
          )}
        </button>

        {/* 系统状态 */}
        {!collapsed && (
          <div className="flex items-center justify-between px-3 py-2 text-xs text-gray-500">
            <div className="flex items-center gap-1.5">
              <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
              <span>系统正常</span>
            </div>
            <span className="text-gray-600">v2.0.0</span>
          </div>
        )}
      </div>
    </aside>
  );
};

export default Sidebar;
