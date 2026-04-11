/**
 * 顶部栏组件
 * 参考 Datadog 设计风格
 */
import React, { useState } from 'react';
import { RefreshCw, Bell, User } from 'lucide-react';
import TimeRangePicker from '../TimeRangePicker';

interface HeaderProps {
  title?: string;
  onRefresh?: () => void;
}

const Header: React.FC<HeaderProps> = ({ title, onRefresh }) => {
  const [timeRange, setTimeRange] = useState<string>('1h');

  return (
    <header className="h-14 bg-[var(--app-surface)]/90 backdrop-blur border-b border-[var(--app-border)] flex items-center justify-between px-4">
      {/* 左侧：页面标题 */}
      <div className="flex items-center">
        {title && <h1 className="text-lg font-semibold text-gray-900">{title}</h1>}
      </div>

      {/* 右侧：工具栏 */}
      <div className="flex items-center space-x-3">
        {/* 时间范围选择器 */}
        <TimeRangePicker value={timeRange} onChange={setTimeRange} />

        {/* 刷新按钮 */}
        <button
          onClick={onRefresh}
          className="p-2 text-[var(--app-text-muted)] hover:text-[var(--app-text)] hover:bg-[var(--app-surface-muted)] rounded-lg transition-colors"
          title="刷新"
        >
          <RefreshCw className="w-5 h-5" />
        </button>

        {/* 通知 */}
        <button className="p-2 text-[var(--app-text-muted)] hover:text-[var(--app-text)] hover:bg-[var(--app-surface-muted)] rounded-lg transition-colors relative">
          <Bell className="w-5 h-5" />
          <span className="absolute top-1 right-1 w-2 h-2 bg-red-500 rounded-full" />
        </button>

        {/* 用户头像 */}
        <button className="flex items-center space-x-2 p-2 hover:bg-[var(--app-surface-muted)] rounded-lg transition-colors">
          <div className="w-8 h-8 bg-[var(--app-accent)] rounded-full flex items-center justify-center">
            <User className="w-5 h-5 text-white" />
          </div>
        </button>
      </div>
    </header>
  );
};

export default Header;
