"""DeepSeek 风格鲸鱼 SVG 图标。

深色 #0A1930 圆角底 + #4D6BFD 主色鲸鱼（流线身体 + 上扬尾巴 + 喷水）。
"""
WHALE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#0D2145"/>
      <stop offset="1" stop-color="#0A1930"/>
    </linearGradient>
  </defs>
  <rect width="256" height="256" rx="52" fill="url(#bg)"/>
  <!-- 鲸鱼身体 -->
  <path d="M58 138 C58 108 118 96 178 116 C196 124 208 132 220 138
           C208 146 194 152 178 158 C118 180 58 168 58 138 Z" fill="#4D6BFD"/>
  <!-- 尾巴（右上卷） -->
  <path d="M214 138 C230 122 244 116 252 120 C246 132 240 142 236 150
           C242 156 246 162 248 170 C238 162 226 154 216 148 Z" fill="#4D6BFD"/>
  <!-- 喷水 -->
  <path d="M92 114 C86 92 96 78 90 60 C102 76 106 94 106 112 C101 115 96 115 92 114 Z" fill="#4D6BFD"/>
  <!-- 眼睛 -->
  <circle cx="104" cy="132" r="5" fill="#0A1930"/>
  <!-- 高光 -->
  <path d="M84 116 C100 108 124 106 148 112" stroke="#7C96FF" stroke-width="6" stroke-linecap="round" fill="none" opacity="0.55"/>
</svg>
"""
