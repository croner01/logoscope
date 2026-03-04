/**
 * 应用主入口 - 配置路由和全局布局
 */
import React, { Suspense, lazy } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import AppLayout from './components/common/Layout/AppLayout';

const Dashboard = lazy(() => import('./pages/Dashboard'));
const LogsExplorer = lazy(() => import('./pages/LogsExplorer'));
const TracesExplorer = lazy(() => import('./pages/TracesExplorer'));
const TopologyPage = lazy(() => import('./pages/TopologyPage'));
const AlertCenter = lazy(() => import('./pages/AlertCenter'));
const AIAnalysis = lazy(() => import('./pages/AIAnalysis'));
const AICaseManagement = lazy(() => import('./pages/AICaseManagement'));
const LabelsDiscovery = lazy(() => import('./pages/LabelsDiscovery'));
const Settings = lazy(() => import('./pages/Settings'));

const RouteFallback: React.FC = () => (
  <div className="flex h-full min-h-[280px] items-center justify-center px-6">
    <div className="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm">
      页面加载中...
    </div>
  </div>
);

const renderRoute = (element: React.ReactNode) => (
  <Suspense fallback={<RouteFallback />}>
    {element}
  </Suspense>
);

const App: React.FC = () => {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route element={<AppLayout />}>
          <Route path="/dashboard" element={renderRoute(<Dashboard />)} />
          <Route path="/logs" element={renderRoute(<LogsExplorer />)} />
          <Route path="/traces" element={renderRoute(<TracesExplorer />)} />
          <Route path="/topology" element={renderRoute(<TopologyPage />)} />
          <Route path="/alerts" element={renderRoute(<AlertCenter />)} />
          <Route path="/ai-analysis" element={renderRoute(<AIAnalysis />)} />
          <Route path="/ai-cases" element={renderRoute(<AICaseManagement />)} />
          <Route path="/labels" element={renderRoute(<LabelsDiscovery />)} />
          <Route path="/settings" element={renderRoute(<Settings />)} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
};

export default App;
