/**
 * 首页 Hero 插画（Step 8B-5）—— 纯装饰、指针忽略。
 *
 * 抽象「会议工作台」：浮起的转写卡片 + 波形 live 线 + 呼吸圆点 + 植物。
 * 不用人形 / 会议头像（避免 fake attendees）；全部由 CSS 渐变 + SVG 形状构成。
 * 对环境 light 与 reduced-motion 友好：无位移动画、无闪烁。
 */
export function HeroIllustration() {
  return (
    <svg
      width="400"
      height="300"
      viewBox="0 0 400 300"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden
    >
      <defs>
        <linearGradient id="hiCard" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#FFFFFF" />
          <stop offset="1" stopColor="#F4F9FF" />
        </linearGradient>
        <linearGradient id="hiAccent" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#3478F6" />
          <stop offset="1" stopColor="#20B486" />
        </linearGradient>
        <linearGradient id="hiBlob" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#E6F2FF" stopOpacity="0.9" />
          <stop offset="1" stopColor="#E6F7F2" stopOpacity="0.6" />
        </linearGradient>
      </defs>

      {/* 背景柔光圆点（水华涟漪） */}
      <circle cx="120" cy="60" r="10" fill="#D9E9FF" opacity="0.55" />
      <circle cx="330" cy="46" r="6" fill="#C9F0E4" opacity="0.7" />
      <circle cx="352" cy="230" r="14" fill="#E6EFFE" opacity="0.6" />
      <circle cx="60" cy="236" r="8" fill="#D7F5EC" opacity="0.65" />

      {/* 波形 live 曲线（转写信号） */}
      <path
        d="M64 176 C 90 164, 108 152, 132 152 C 156 152, 168 186, 192 184 C 216 182, 224 150, 248 152 C 272 154, 286 178, 312 172"
        stroke="url(#hiAccent)"
        strokeWidth="3"
        strokeLinecap="round"
        fill="none"
      />
      {/* 波形上方的呼吸点（实时信号） */}
      <circle cx="312" cy="172" r="5" fill="#20B486" />

      {/* 主转写卡片（会议工作台） */}
      <rect x="104" y="88" width="220" height="128" rx="16" fill="url(#hiCard)" stroke="#E8EEF5" />
      {/* 卡片顶栏：标题线 + 小 live 点 */}
      <rect x="126" y="106" width="76" height="6" rx="3" fill="#DDE7F5" />
      <circle cx="292" cy="109" r="4" fill="#20B486" opacity="0.9" />
      {/* 卡片正文：FINAL 行（深色）+ PARTIAL 行（浅色） */}
      <rect x="126" y="124" width="176" height="5" rx="2.5" fill="#B7C4D8" opacity="0.7" />
      <rect x="126" y="138" width="150" height="5" rx="2.5" fill="#DDE7F5" />
      <rect x="126" y="152" width="164" height="5" rx="2.5" fill="#CDEAF0" />
      {/* 卡片底部状态条：AI 纪要徽标 */}
      <rect x="126" y="178" width="64" height="18" rx="9" fill="#E6F7F2" />
      <rect x="138" y="184" width="40" height="6" rx="3" fill="#20B486" opacity="0.75" />

      {/* 次级小卡片：纪要摘要卡，叠在主卡左后下方 */}
      <rect
        x="64"
        y="156"
        width="96"
        height="64"
        rx="12"
        fill="#FFFFFF"
        stroke="#E8EEF5"
        transform="rotate(-6 112 188)"
      />
      <rect x="76" y="176" width="56" height="5" rx="2.5" fill="#DDE7F5" transform="rotate(-6 104 178)" />
      <rect x="76" y="190" width="40" height="5" rx="2.5" fill="#E4EEF9" transform="rotate(-6 96 192)" />
      <circle cx="142" cy="200" r="4" fill="#20B486" opacity="0.5" transform="rotate(-6 142 200)" />

      {/* 植物：左侧一株细叶，右侧一株小枝（AI 工作台的自然点缀） */}
      <g stroke="#9CC9B4" strokeWidth="2.5" strokeLinecap="round">
        <path d="M44 232 C 44 210, 52 196, 62 184" />
        <path d="M54 216 C 60 200, 72 194, 84 190" />
        <path d="M40 214 C 34 200, 34 190, 36 178" />
        <path d="M300 246 C 304 236, 312 228, 322 222" />
        <path d="M310 236 C 314 228, 322 222, 332 220" />
      </g>
      <g fill="#BFE3D4">
        <circle cx="62" cy="184" r="3" />
        <circle cx="84" cy="190" r="2.6" />
        <circle cx="36" cy="178" r="2.6" />
        <circle cx="322" cy="222" r="2.6" />
        <circle cx="332" cy="220" r="2.2" />
      </g>

      {/* 底座柔光 blob（水面意象） */}
      <ellipse cx="164" cy="262" rx="120" ry="14" fill="url(#hiBlob)" opacity="0.7" />
    </svg>
  )
}
