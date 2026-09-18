/**
 * Design Tokens —— 视觉「轻量、克制、现代 AI Meeting Workspace」的唯一数据源。
 * （fronted_designer §47 色值 + 本次 Visual Polish）
 *
 * 规则：
 *  - 组件 / 页面禁止散落 magic color / radius / spacing；
 *  - Ant Design 组件色经 ConfigProvider theme（见 antdTheme）注入；
 *  - 结构布局类 CSS 见 global.css，使用这里的 CSS 变量。
 */
export const designTokens = {
  // Brand —— 克制使用：主 CTA、激活态、live 指示
  colorPrimary: '#2F6BFF',
  colorPrimaryHover: '#4C7DFF',
  colorPrimaryActive: '#1F5AE0',
  colorPrimaryLight: '#EEF4FF',

  // Semantic
  colorSuccess: '#22B573',
  colorWarning: '#F5A623',
  colorDanger: '#F04438',

  // Neutrals —— 安静、高可读
  colorBgPage: '#F7F9FC',
  colorBgSurface: '#FFFFFF',
  colorBgSubtle: '#F2F4F8',
  colorTextPrimary: '#1F2937',
  colorTextSecondary: '#5F6B7A',
  colorTextTertiary: '#98A2B3',
  colorBorder: '#E4E7EC',
  colorBorderLight: '#EEF0F4',

  // Radius —— 统一、收敛
  radiusSm: 8,
  radiusMd: 10,
  radiusLg: 14,

  // Layout
  sidebarWidth: 220,
  contentMaxWidth: 1080,

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

    colorBgLayout: designTokens.colorBgPage,
    colorBgContainer: designTokens.colorBgSurface,
    colorBgElevated: designTokens.colorBgSurface,

    colorText: designTokens.colorTextPrimary,
    colorTextSecondary: designTokens.colorTextSecondary,
    colorTextTertiary: designTokens.colorTextTertiary,

    colorBorder: designTokens.colorBorder,
    colorBorderSecondary: designTokens.colorBorderLight,

    borderRadius: designTokens.radiusMd,
    borderRadiusSM: designTokens.radiusSm,
    borderRadiusLG: designTokens.radiusLg,

    fontSize: 14,
    controlHeight: 32,
    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Helvetica Neue', Helvetica, Arial, sans-serif",
  },
  components: {
    Layout: {
      headerBg: designTokens.colorBgSurface,
      siderBg: designTokens.colorBgSurface,
      bodyBg: designTokens.colorBgPage,
    },
    Menu: {
      itemBorderRadius: designTokens.radiusSm,
      itemSelectedBg: designTokens.colorPrimaryLight,
      itemSelectedColor: designTokens.colorPrimary,
      itemHeight: 38,
      itemMarginInline: 8,
      itemMarginBlock: 4,
    },
    Button: {
      fontWeight: 500,
    },
    Tabs: {
      itemSelectedColor: designTokens.colorPrimary,
      inkBarColor: designTokens.colorPrimary,
    },
  },
} as const
