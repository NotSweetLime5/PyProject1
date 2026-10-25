import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 端口固化：本项目后端 8200、前端 5274，写死在代码里而不是靠命令行参数或记忆。
// 之前一直混乱是因为这两个值散落在"我当时传了什么 flag"里，而同一台机器上还跑着
// 另一个项目（8000 + 5173/5175/5176），端口撞了 vite 会静默跳到下一个空闲口，
// 于是浏览器里看到的可能是另一个项目的页面。
// 用 --strictPort：端口被占时直接报错退出，绝不静默漂移。
const BACKEND = process.env.VITE_BACKEND || 'http://127.0.0.1:8200'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5274,
    strictPort: true,
    host: '127.0.0.1',
    proxy: {
      // 开发期把 /api 转发到后端，浏览器看到的是同源，不会被 CORS 拦住。
      // 生产由 nginx 反代同样的路径，所以两边都不依赖后端的 CORS 配置
      // （后端的 CORS 是给"前端跑在别的域名下"这种场景兜底的）。
      '/api': {
        target: BACKEND,
        changeOrigin: true,
        // SSE 必须关缓冲：否则代理会把流式响应攒到结束才吐给浏览器，
        // 表现就是"聊天界面卡住不动，最后一次性蹦出整段回答"
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              proxyRes.headers['x-accel-buffering'] = 'no'
            }
          })
        },
      },
    },
  },
})
