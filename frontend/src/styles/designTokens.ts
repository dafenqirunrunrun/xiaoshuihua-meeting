/**
 * Design Tokens —— 视觉「轻量、克制、现代 AI Meeting Workspace」的唯一数据源。
 * （fronted_designer §47 色值 + Step 8B-5 Visual Polish：中性内容工作台 + 薄荷呼吸点）
 *
 * 规则：
 *  - 组件 / 页面禁止散落 magic color / radius / spacing；
 *  - Ant Design 组件色经 ConfigProvider theme（见 antdTheme）注入；
 *  - 结构布局类 CSS 见 global.css，使用这里的 CSS 变量。
 */
export const designTokens = {
  // Brand —— 克制使用：主 CTA、激活态、live 指示
  colorPrimary: '#F43F5E',
  colorPrimaryHover: '#FB5B73',
  colorPrimaryActive: '#E11D48',
  colorPrimaryLight: '#FFF1F3',

  // AI / Live —— 只在「实时 / AI / 就绪」信号使用，绝不用于纯 decoration
  colorMint: '#20B486',
  colorMintLight: '#E6F7F2',

  // Semantic
  colorSuccess: '#20B486',
  colorWarning: '#F5A623',
  colorDanger: '#F04438',

  // Neutrals —— 暖白页面、纯白内容面、近黑正文
  colorBgPage: '#F5F4F6',
  colorBgSurface: '#FFFFFF',
  colorBgSubtle: '#F2F2F3',
  colorTextPrimary: '#202124',
  colorTextSecondary: '#55575D',
  colorTextTertiary: '#92949A',
  colorBorder: '#E5E2E6',
  colorBorderLight: '#EFEDF0',

  // Radius —— 内容产品保持紧凑，圆形只留给头像和状态点
  radiusSm: 6,
  radiusMd: 8,
  radiusLg: 8,

  // Shadow —— 非常弱，hover 略升；边框优先于阴影
  shadowCard:
    '0 1px 2px rgba(46, 38, 42, 0.025), 0 10px 30px rgba(46, 38, 42, 0.055)',
  shadowCardHover:
    '0 2px 5px rgba(46, 38, 42, 0.035), 0 16px 38px rgba(46, 38, 42, 0.08)',

  // Layout
  sidebarWidth: 208,
  contentMaxWidth: 1200,
  railWidth: 300,

  // Motion —— 统一时长，克制使用
  motionFast: 160,
  motionBase: 200,

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
      itemBorderRadius: designTokens.radiusMd,
      itemSelectedBg: designTokens.colorPrimaryLight,
      itemSelectedColor: designTokens.colorPrimary,
      itemHoverBg: designTokens.colorBgSubtle,
      itemHeight: 44,
      itemMarginInline: 10,
      itemMarginBlock: 4,
    },
    Button: {
      fontWeight: 500,
      borderRadius: designTokens.radiusSm,
      contentFontSize: 14,
    },
    Card: {
      borderRadiusLG: designTokens.radiusLg,
    },
    Tabs: {
      itemSelectedColor: designTokens.colorPrimary,
      inkBarColor: designTokens.colorPrimary,
    },
  },
} as const
