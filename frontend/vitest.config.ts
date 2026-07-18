import { defineConfig } from 'vitest/config'

// 质量基线(2026-07-18):单元测试配置。
// 默认 node 环境(纯逻辑/store 测试无需 DOM);需要 DOM 的组件测试再按需切 jsdom。
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
