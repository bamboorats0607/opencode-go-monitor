# OpenCode GO 用量监控 V2

Python 原生 GUI（PySide6）桌面监控软件，核心目标是**诊断缓存命中问题**（打分 + 告警）。V2 为深色仪表盘 UI（参考深色仪表盘设计图），新增日志、端口退避、完整设置面板、状态指示器与可靠性保障。

**数据源（V2.1 新增）**：默认以 **go 订阅在线控制台全量调用**（`opencode.ai/workspace/{id}/usage` 的 usage.list）为诊断主数据源——该列表经 go 订阅推理网关（provider `inf-go.oa-compat`）统一记录**所有客户端**（opencode CLI、Trae 等）的每次调用；本地 `opencode.db` 仅在在线不可用时回退（仅覆盖 opencode CLI）。

## 功能（V2）

| 功能 | 说明 |
|---|---|
| 深色仪表盘 UI | 顶部栏（Logo+标题+副标题 + 24h/7d/30d 时间切换 + 在线状态点 + 刷新）+ 4 KPI 卡（缓存健康度色环 / Token 总量 / 估算成本 / 异常告警数）+ 订阅三窗口进度条（5h 橙/周 紫/月 蓝）+ 最近请求表格 |
| **缓存健康度打分** | 0-100 分：`Score = round(60·sub_HD + 20·sub_V + 10·sub_S + 10·sub_C)`；窗口命中率 + 骤降惩罚 + 剩余额度 + 数据源健康 + cache_write 维度（恒 0 时按 /0.9 归一化标"不可测"）；等级 健康/良好/关注/异常 |
| **缓存特征检测** | 比例阈值：单条 `input>10k` 且 `cache_read/input<1%`；同 session 前 10 条高命中(≥90%)自动抑制（防大段粘贴假阳性）；连续 ≥3 条升级 Critical |
| **三类告警** | 缓存健康（打分<55/特征检测/骤降）、额度（5h/周/月 ≥70%/90%/100%）、数据源（在线 401/额度拉取失败/db 只读失败/SSE 挂）；cooldown 30min/类；横幅主通道 + 托盘气泡可选 |
| **在线全量调用监控** | `opencode.ai/workspace/{id}/usage` SSR usage.list：逐条 model/input/output/cacheRead/cost/sessionID/keyID；**覆盖所有客户端（含 Trae）**；60s 增量合并，低命中标红置顶；在线可用时 KPI/诊断/请求表均以在线全量为准 |
| 在线三窗口额度 | `opencode.ai/workspace/{id}/go` SSR 数据：5h/周/月 + 余额；Cookie 可本地代理自动捕获 / Edge 扩展直读 / 手动粘贴 |
| 本地只读监控（回退） | `mode=ro` 只读打开 opencode.db，全程零写入；三级聚合 Session/小时/模型，命中率 = `cache.read / (input + cache.read)` |
| 实时等效 | 500ms 文件 mtime+size 监听 + 增量查询 + 5s `PRAGMA data_version` 兜底（WAL 模式） |
| **端口退避（双监听）** | ① 主监听 8899 被占自动退避：8899→8910→8920→8930→8940（≤5 次）② 扩展专用口 8900 **固定**，被占硬提示（扩展永不失联） |
| **请求失败指数退避** | 手写 1s→2s→4s→8s→16s（≤5 次）；仅 5xx/网络错误重试，**401/403 不重试**（禁止 tenacity） |
| **完整设置面板** | 四 Tab（通用/阈值/通知/日志）：workspace ID、端口池、扩展端口、DB 轮询间隔、在线拉取间隔、时间范围、全部诊断阈值、托盘气泡开关、日志文件与保留天数；保存即时热加载 |
| **日志** | 文件按天滚动（`TimedRotatingFileHandler` midnight，backupCount=7）+ UI 日志面板（Signal 跨线程，上限 5000 行，按级别着色） |
| **可靠性** | QLockFile 单实例（双开第二实例被拒）；SSE 反代心跳看门狗（超时 2×interval 自动重启）；组件 try/except + 状态指示器（绿/橙/红）；进程感知（opencode 未运行横幅，非故障） |
| 托盘驻留 | 关闭窗口最小化到系统托盘（鲸鱼图标），双击恢复，菜单可退出 |

## 环境要求

- Python 3.10+（开发环境：`D:\ide\python12\python.exe`，3.12.9）
- Windows（进程检测依赖 `tasklist`）

## 安装与运行

```powershell
cd D:\python\opencode-go-monitor
D:\ide\python12\python.exe -m pip install -r requirements.txt
D:\ide\python12\python.exe main.py
```

自定义数据库路径（默认 `C:\Users\dgx19\.local\share\opencode\opencode.db`）：

```powershell
D:\ide\python12\python.exe main.py --db "D:\path\to\opencode.db"
```

单实例保护：重复启动会弹窗提示"程序已在运行"并退出。

## 在线用量与全量调用监控

Go 订阅用量系统与主控制台是**两个独立系统**。本软件拉取两个页面：

1. `opencode.ai/workspace/{id}/go` — 5h/周/月三窗口额度百分比 + 余额
2. `opencode.ai/workspace/{id}/usage` — **全部客户端调用明细**（usage.list SSR，含 opencode CLI、Trae 等经 go 订阅网关的每次调用：model/input/output/cacheRead/cost/sessionID）；**Cookie 有效时作为"最近请求"表格与诊断打分的主数据源**，每 60s 增量合并

### 方式一：Edge 浏览器扩展（推荐，无需改代理）

1. 程序运行中（自动监听 `127.0.0.1:8900` 接收端）
2. 安装扩展（见下方"扩展安装"）
3. 在 Edge 中登录 opencode.ai 并打开 Go 用量页
4. 点击扩展图标 →「捕获 Cookie 并发送」→ 程序自动保存并拉取用量

**扩展安装**（本地加载，无需商店）：

1. Edge 打开 `edge://extensions` → 打开"开发人员模式"
2. 点「加载解压缩的扩展」→ 选择目录 `D:\python\opencode-go-monitor\extension`
3. 扩展「OpenCode GO Cookie 捕获」出现在工具栏，固定图标即可

> 扩展使用浏览器 `chrome.cookies` API 直读 opencode.ai 的 auth cookie
> （浏览器自己解密，扩展合法访问），POST 到本机程序。不读取 Cookie 数据库、不解密、不提权。

### 方式二：本地代理自动捕获 Cookie

1. 点击本软件右上角「**启用代理捕获**」（监听 `127.0.0.1:8899`，被占时自动退避 8899→8910→…）
2. 在浏览器（Edge/Chrome）设置 → 代理，把 HTTP/HTTPS 代理指向程序显示的实际端口
3. 在浏览器访问 opencode.ai 的 Go 用量页（如 https://opencode.ai/workspace/wrk_01KZAA0VA6E8JQNDSPW60RX3K8/go）
4. 程序自动捕获 `auth` cookie 并保存，自动拉取用量 → 顶部三窗口进度条实时显示

> 代理仅捕获 opencode.ai 域的 auth cookie，其余流量原样透明转发；**不做任何中间人解密**，不读取、不解密浏览器 Cookie 数据库。
> 用完可点「停止代理」恢复浏览器原代理设置。

### 方式三：手动粘贴 Cookie

1. 在浏览器登录 opencode.ai，打开 Go 用量页
2. F12 → Network → 刷新 → 点击任意请求 → Headers → Request Headers → 复制 `Cookie:` 中 `auth=...` 的值（`Fe26.2**` 开头）
3. 点击「设置 Cookie」→ 粘贴 → 「测试连接」→ 保存

### 失效处理

- 界面出现橙色横幅「在线 API Cookie 已失效（401/403）」→ 重新登录 opencode.ai（Cookie 过期），或重新启用代理捕获；状态点变橙色，不影响本地诊断
- Cookie 保存在本机 QSettings（仅代理捕获或手动粘贴，程序不做任何自动导出/解密/提权）

## 诊断打分与告警说明

- **打分**：缓存健康度 KPI 卡显示 0-100 分与等级色环（绿健康/蓝良好/橙关注/红异常）。分数综合最近 20 条消息窗口命中率、命中率骤降、剩余订阅额度、数据源健康度、cache_write 维度。
- **特征检测**：低命中消息（input>10k 且命中率<1%）在请求表格标红置顶；连续 3 条触发 → Critical 告警；大段粘贴场景（前 10 条高命中）自动降级为提示，不误报。
- **告警横幅**：顶部横幅按类显示（红=缓存健康、橙=额度、黄=数据源）；托盘气泡可选（设置 → 通知）。
- 告警冷却 30 分钟/类，防刷屏；三类可独立开关。

## 测试

```powershell
D:\ide\python12\python.exe tests\test_parser_aggregator.py   # 解析/聚合断言（26 项）
D:\ide\python12\python.exe tests\test_score.py               # 打分+特征检测回归（25 项，样本 B 必触发）
D:\ide\python12\python.exe tests\verify_readonly.py          # 只读校验（打开前后 hash 不变）
```

## 打包（PyInstaller，dev）

```powershell
D:\ide\python12\python.exe -m pip install pyinstaller
D:\ide\python12\python.exe -m PyInstaller --noconfirm --onefile --windowed ^
  --add-data "assets\whale.svg;assets" ^
  --name "OpenCodeGoMonitor" main.py
```

图标内嵌于源码（`src/ui/whale_svg.py`），单 exe 无外部路径依赖。

## 技术说明与红线

- **禁止 SSE /event 主通道**：桌面版 OpenCode（v1.18.15）无 CLI serve 证据；程序仅做限时尽力探测 + 看门狗，失败标注"未实现"，以本地文件监听等效实时流
- **禁止写 opencode.db**：仅 `URI mode=ro` + `PRAGMA query_only=ON` 双保险
- **禁止 cookie 解密/自动导出**：Cookie 仅通过本地代理捕获（透明转发捕获请求头）、Edge 扩展直读或手动粘贴获取，不读取浏览器 Cookie 数据库、不做任何解密/提权
- **禁止 tenacity**：请求退避为手写指数退避（1→2→4→8→16s）
- 未引入 Tkinter/matplotlib；无三级系统告警、无静默时段配置项；请求表格无逐条打分列；扩展口 8900 不参与退避池

## 已知限制

- 命中率公式为 `cache.read/(input+cache.read)`，与 opencode 计费口径可能存在差异（计费或使用 `total` 等字段），仅用于本软件内部诊断对比
- WAL 模式下若 `-shm` 文件缺失（opencode 从未运行的冷环境），自动退回 `immutable` 只读主库文件，数据可能非最新
- 90 天在线趋势、priming 分析不在本次范围内
