// 质量基线(2026-07-18):ESLint flat config(Vue3 + TS)。
// 规则分层:js 推荐 + TS 推荐 + Vue 推荐,最后用 prettier 关闭所有格式化规则,
// 避免 eslint 与 prettier 在格式上打架(格式化交由 prettier 统一管理)。
import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import pluginVue from 'eslint-plugin-vue'
import prettier from 'eslint-config-prettier'

export default [
  {
    ignores: ['dist', 'node_modules', 'coverage', '*.config.ts', '*.config.js'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  // .vue 的 <script> 用 TS 解析器,否则模板里的 TS 类型不检查
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
      },
    },
  },
  prettier,
  {
    rules: {
      // 现有代码有用 any 的务实写法,先放行;后续逐步收紧
      '@typescript-eslint/no-explicit-any': 'off',
      'vue/multi-word-component-names': 'off',
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
    },
  },
]
