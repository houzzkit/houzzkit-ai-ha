# 小瑞 Agent 智能音箱配套的 Home Assistant 自定义集成

HOUZZkit AI HA 是 **Home Assistant 的自定义集成**，隶属于「小瑞 Agent」项目，核心目标是打通「小瑞 Agent 智能音箱」与 Home Assistant 的联动，让智能音箱成为 HA 的 “智能控制入口”，实现对所有 HA 关联设备的便捷控制。

## 一、核心能力



1. **无缝控制 HA 全量设备（MCP 技术支撑）**

   借助先进的 **MCP（Model Context Protocol，模型上下文协议）**，可直接将小瑞 Agent 智能音箱作为 HA 语音助手，控制 HA 中公开给语音助手的所有设备（如灯光、空调、传感器等）。

2. **ESPHome 深度整合**

   小瑞 Agent 设备功能（如音箱音量、禁麦等）会以 **HA 标准实体** 形式展示在 HA 界面中：

* 可通过 HA 界面直接控制小瑞 Agent（如调节音量、切换模式）；

* 可将小瑞 Agent 纳入 HA 自动化规则。

## 二、安装指南

### 推荐方案：通过 HACS 快速安装（更便捷，优先选择）

前提：确保你的 Home Assistant 已安装 [ HACS（Home Assistant 社区商店）](https://hacs.xyz/)



1. 打开 Home Assistant 后台，进入 **HACS 商店**（左侧菜单栏找到「HACS」）；

2. 在 HACS 中切换到「集成（Integrations）」分类，点击右上角搜索框，输入 **HOUZZkit AI**；

3. 点击搜索结果中的「[HOUZZkit AI](https://my.home-assistant.io/redirect/hacs_repository/?category=integration&owner=houzzkit&repository=houzzkit-ai)」，选择「安装」，等待安装完成后，**重启 Home Assistant**（必须重启才能生效）。

### 备选方案：手动安装（无 HACS 时使用）



1. 访问 [HOUZZkit AI HA 代码仓库](https://github.com/houzzkit/houzzkit-ai-ha)，点击「Code」→「Download ZIP」下载插件压缩包；

2. 解压压缩包，将名为 `custom_components/houzzkit_ai` 的文件夹，复制到你的 Home Assistant 根目录下的 `custom_components` 文件夹中（若没有 `custom_components` 文件夹，需手动创建）；

3. 重启 Home Assistant。

## 三、配置与使用

1. 重启 HA 后，进入 **设置 → 设备与服务 → 集成**；

2. 点击右下角「+ 添加集成」，在搜索框中输入 **HOUZZkit AI** 并选择；

3. 使用「小瑞 Agent 微信小程序」扫描弹窗中的二维码，即可完成绑定。

## 四、MCP 能力说明

支持所有 HA 官方提供的意图，具体可参考 [HA 官方意图文档](https://developers.home-assistant.io/docs/intent_builtin/)

除官方意图外，已支持的自定义意图如下：

- [x] 调节空调模式
- [x] 调节空调风量
