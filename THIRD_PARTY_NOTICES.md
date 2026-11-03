# 第三方组件

本项目源代码使用 MIT License。桌面包同时分发了 Python、Playwright 的浏览器驱动及其它依赖，它们按各自的许可证提供；项目的 MIT License 不改变这些组件的许可条件。

构建脚本会收集 Python 的 LICENSE.txt，以及 Flask、Playwright、Werkzeug、Jinja2、Click、ItsDangerous、MarkupSafe、greenlet、pyee 和 tzdata 的发行元数据。Playwright 分发中的 Node.js、驱动及其许可文件也保留在原目录。请连同 `_internal` 和许可证文件一起分发桌面包。

Chrome、Edge 和系统 Chromium 不随桌面包分发，由使用者自行安装。DeepSeek、豆包、Kimi 的名称仅用于说明兼容的平台，本项目与这些平台无隶属关系。
