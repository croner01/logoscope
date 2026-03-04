/**
 * 应用主布局 - 侧边栏 + 内容区
 * 支持侧边栏收起/展开，参考 Datadog 设计风格
 */
import React, { useState, useCallback } from 'react';
import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import Header from './Header';

const AppLayout: React.FC = () => {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const handleCollapseChange = useCallback((collapsed: boolean) => {
    setSidebarCollapsed(collapsed);
  }, []);

  return (
    <div className="flex h-screen bg-[var(--app-bg)] text-[var(--app-text)]">
      {/* 侧边栏 */}
      <Sidebar 
        collapsed={sidebarCollapsed}
        onCollapseChange={handleCollapseChange}
      />

      {/* 主内容区 */}
      <div className="flex-1 flex flex-col overflow-hidden min-w-0">
        {/* 顶部栏 */}
        <Header />

        {/* 内容区域 */}
        <main className="flex-1 overflow-auto bg-transparent">
          <Outlet />
        </main>
      </div>
    </div>
  );
};

export default AppLayout;
