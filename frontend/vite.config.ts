import { defineConfig, loadEnv, type ProxyOptions } from 'vite';
import react from '@vitejs/plugin-react';

function buildProxy(target: string, ws = false): ProxyOptions {
  return {
    target,
    changeOrigin: true,
    secure: false,
    ws,
    // 保持路径不重写，和生产 nginx 规则一致
    rewrite: (path) => path,
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');

  // INT-02: 与生产 nginx 保持同样的按路径分流策略
  const QUERY_SERVICE_TARGET = env.VITE_QUERY_SERVICE_TARGET || 'http://localhost:8081';
  const TOPOLOGY_SERVICE_TARGET = env.VITE_TOPOLOGY_SERVICE_TARGET || 'http://localhost:8082';
  const SEMANTIC_ENGINE_TARGET = env.VITE_SEMANTIC_ENGINE_TARGET || 'http://localhost:8080';

  return {
    plugins: [react()],
    server: {
      port: 3000,
      host: '0.0.0.0',
      open: true,
      proxy: {
        // Query Service
        '/api/v1/logs': buildProxy(QUERY_SERVICE_TARGET),
        '/api/v1/metrics': buildProxy(QUERY_SERVICE_TARGET),
        '/api/v1/traces': buildProxy(QUERY_SERVICE_TARGET),
        '/api/v1/data-quality': buildProxy(QUERY_SERVICE_TARGET),
        '/api/v1/trace-lite': buildProxy(QUERY_SERVICE_TARGET),
        '/api/v1/quality': buildProxy(QUERY_SERVICE_TARGET),
        '/api/v1/value': buildProxy(QUERY_SERVICE_TARGET),
        '/ws/logs': buildProxy(QUERY_SERVICE_TARGET, true),

        // Topology Service
        '/api/v1/graph': buildProxy(TOPOLOGY_SERVICE_TARGET),
        '/api/v1/topology': buildProxy(TOPOLOGY_SERVICE_TARGET, true),
        '/api/v1/monitor': buildProxy(TOPOLOGY_SERVICE_TARGET),
        '/ws/topology': buildProxy(TOPOLOGY_SERVICE_TARGET, true),
        '/ws/status': buildProxy(TOPOLOGY_SERVICE_TARGET),

        // Semantic Engine
        '/api/v1/ai': buildProxy(SEMANTIC_ENGINE_TARGET),
        '/api/v1/alerts': buildProxy(SEMANTIC_ENGINE_TARGET),
        '/api/v1/labels': buildProxy(SEMANTIC_ENGINE_TARGET),
        '/api/v1/cache': buildProxy(SEMANTIC_ENGINE_TARGET),
        '/api/v1/deduplication': buildProxy(SEMANTIC_ENGINE_TARGET),
        '/health': buildProxy(SEMANTIC_ENGINE_TARGET),
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: true,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (!id.includes('node_modules')) {
              return undefined;
            }
            if (id.includes('react-router-dom')) {
              return 'vendor-router';
            }
            if (id.includes('react-window')) {
              return 'vendor-virtual-list';
            }
            if (id.includes('axios')) {
              return 'vendor-network';
            }
            if (id.includes('lucide-react')) {
              return 'vendor-icons';
            }
            return 'vendor-core';
          },
        },
      },
    },
  };
});
