GEO 诊断台：本机三平台试用版

请选择与操作系统、处理器匹配的包，完整解压后运行：
- Windows：双击 GEO诊断台.exe，保留旁边的 _internal。
- macOS：将 GEO诊断台.app 放入「应用程序」后打开。Apple 芯片与 Intel 使用各自的包。
- Linux 桌面：运行 geo-diagnostic；也可在桌面终端运行 ./启动GEO.sh。

界面打开后，输入品牌名创建诊断项目；品类和竞品均可选填。
程序会直接进入诊断采集页并选好第一条起始问题。首次使用时打开平台登录。
在程序打开的独立 Chrome 或 Edge 中自行登录 DeepSeek、豆包、Kimi。
Linux 也会尝试已安装的系统 Chromium。密码和验证码只在官网输入。
回到 GEO，逐个平台选择问题采集；核对排名、评价与引用后查看统计、保存 PDF。
「导出证据包」可带走 CSV、原回答及截图；「退出 GEO」会保存浏览器资料并关闭服务。

收件人无需安装 Python、Node 或 AI 工具，但需要浏览器、可访问平台官网的网络和自己的账号。
当前只接入上述三个平台，报告需要人工复核，平台账号过期或验证须本人处理。
Linux 需要桌面会话；纯命令行服务器无法直接完成这个原生窗口登录流程。
Linux 包的 glibc 基线与 CPU、Mac 包的 CPU 见 package-manifest.json；不能通用替换。
macOS 未签名测试包可能被系统拦截，正式对外分发还需要 Apple 签名、公证及真机验收。

默认资料位置（旧版 Unix 已有 ~/GeoBrandDiagnostic 时沿用旧位置）：
- Windows：%LOCALAPPDATA%/GeoBrandDiagnostic
- macOS：~/Library/Application Support/GeoBrandDiagnostic
- Linux：${XDG_DATA_HOME:-~/.local/share}/GeoBrandDiagnostic
data 下是项目和证据，browser-profile 下是专用登录资料。
替换程序不会删除用户资料；不要把自己的整个资料目录发给别人。

已构建、自动验证、真实账号与桌面验收是不同状态，请以同包的平台与验收说明、对应验证回执为准。
