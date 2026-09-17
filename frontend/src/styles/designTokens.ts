/**
 * Design Tokens —— fronted_designer.md §47 的统一定义。
 * 所有 UI 组件不写散落色值：Ant Design 组件色经 ConfigProvider 注入，
 * 全局 CSS（styles/global.css）使用这里的 CSS 变量；语义收口到此处。
 */
export const designTokens = {
  colorPrimary: '#2F6BFF',
  colorPrimaryHover: '#4C7DFF',
  colorPrimaryLight: '#EEF4FF',
  colorSuccess: '#22B573',
  colorWarning: '#F5A623',
  colorDanger: '#F04438',
  colorBgPage: '#F7F9FC',
  colorTextPrimary: '#1F2937',
  colorTextSecondary: '#667085',
  colorTextTertiary: '#98A2B3',
  colorBorder: '#E4E7EC',
  radiusSm: 8,
  radiusMd: 12,
  radiusLg: 16,
  spacing: [4, 8, 12, 16, 24, 32, 40, 48] as const,
} as const

/** Ant Design ConfigProvider theme token（全局唯一入口） */
export const antdTheme = {
  token: {
    colorPrimary: designTokens.colorPrimary,
    colorInfo: designTokens.colorPrimary,
    colorSuccess: designTokens.colorSuccess,
    colorWarning: designTokens.colorWarning,
    colorError: designTokens.colorDanger,
    borderRadius: designTokens.radiusMd,
    colorBgLayout: designTokens.colorBgPage,
    colorText: designTokens.colorTextPrimary,
    colorTextSecondary: designTokens.colorTextSecondary,
    colorTextTertiary: designTokens.colorTextTertiary,
    colorBorder: designTokens.colorBorder,
    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Helvetica Neue', Helvetica, Arial, sans-serif",
  },
  components: {
    Layout: {
      headerBg: '#FFFFFF',
      siderBg: designTokens.colorBgPage,
    },
    Menu: {
      itemBorderRadius: designTokens.radiusSm,
      itemSelectedBg: designTokens.colorPrimaryLight,
      itemSelectedColor: designTokens.colorPrimary,
    },
  },
} as const
