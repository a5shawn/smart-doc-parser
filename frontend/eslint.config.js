import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      // 生成物：由 openapi-typescript 与 unplugin-vue-components 产出，不手改也不检查
      'src/api/types.gen.ts',
      'src/components.d.ts',
    ],
  },

  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],

  {
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
      ecmaVersion: 'latest',
      sourceType: 'module',
    },
  },

  {
    // .vue 文件要先由 vue-eslint-parser 拆出 <script>，再交给 TS 解析器
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
        extraFileExtensions: ['.vue'],
      },
    },
  },

  {
    rules: {
      // 组件名用单词（ExtractView、SettingsView…）在这个项目里足够清晰，
      // 强制多词只会带来 ResultPanelCard 这种凑数的名字
      'vue/multi-word-component-names': 'off',
      // 未使用变量报错，但允许用下划线前缀显式标记"这个参数是故意不用的"
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // console.warn/error 是刻意保留的（SSE 断线、组件异常等需要留痕），
      // 只禁止调试时忘了删的 console.log
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      // 模板里的属性换行顺序交给 prettier，lint 不再管
      'vue/attributes-order': 'off',
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
    },
  },

  {
    files: ['**/*.spec.ts'],
    rules: {
      // 测试里大量使用非空断言来读取断言结果，可读性更好
      '@typescript-eslint/no-non-null-assertion': 'off',
    },
  },
)
