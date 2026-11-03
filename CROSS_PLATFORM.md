# 平台与构建说明

普通使用者下载 [Releases](https://github.com/ewanyuan/geo-brand-diagnostic/releases) 中对应系统的程序包，完整解压后运行。源码和浏览器驱动随包构建，不要求使用者安装 Python 或 AI 工具。

## 平台范围

- Windows x86_64：解压后双击「GEO诊断台.exe」。
- Linux x86_64：需要图形桌面和 Chrome、Edge 或系统 Chromium。运行 `geo-diagnostic` 或 `./启动GEO.sh`；构建基线 Debian 12 / glibc 2.36，较旧发行版不在当前包的范围内。
- macOS 与 Linux arm64：只保留配置，未发布已验收的包。Mac 还需完成 Finder 重开事件适配、原生构建和真机验收；不能用命令行重复执行代替 Finder 双击验收。

项目资料在使用者目录，替换程序不会覆盖资料。Unix 旧版已有 `~/GeoBrandDiagnostic` 时保留原位置；具体路径见 README。

## 原生构建

在目标操作系统及目标 CPU 上安装 Python 和 `requirements-desktop-build.txt`，运行 `python deploy/build_desktop.py --platform windows` 或 `--platform linux`。构建会拒绝与宿主不同的操作系统。产物包含文件清单与 SHA256，输出路径为 `dist/<系统>-<CPU>-<版本>/`。

构建所用 Python 发行版必须包含 LICENSE.txt。打包脚本只收集程序资源和依赖，不复制项目数据、账号、截图或个人网络配置。Linux 的 tar.gz 保留执行权限与包内符号链接；系统浏览器不会继承冻结程序的私有库搜索路径。

Linux 隔离构建环境可用 `deploy/Dockerfile.linux-build` 准备。`deploy/verify_linux_container.sh` 接收 `/src` 只读源码、`/packages` 输出目录和 `/reports` 检查目录，在容器内部构建和验收，不应将用户资料挂入容器。

macOS 配置使用 `.app` 包和按 CPU 的原生构建，正式分发还需签名、公证与真机验收；当前不提供可下载的 Mac 成品。

## 验证层级

源码测试、实际程序包检查、虚拟图形显示测试和真实账号验证分别记录。Linux 容器通过不代表每一种 Linux 桌面均已通过；本地测试页面与测试 Cookie 不是平台账号登录。首次使用请在三个平台各采一题，核对原文与截图后再扩大批次。

完整条件见 [ACCEPTANCE.md](ACCEPTANCE.md)，本版本范围见 [docs/VALIDATION.md](docs/VALIDATION.md)。
