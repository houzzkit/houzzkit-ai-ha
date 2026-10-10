# HOUZZkit AI Home Assistant 集成

HOUZZkit AI HA 是「小瑞 Agent」的 Home Assistant 自定义集成。

## 一、核心能力

- **语音控制 Home Assistant 设备**：通过 MCP 控制 Home Assistant 中已公开给语音助手的实体。
- **接入小瑞 Agent 智能音箱**：音箱通过 ESPHome 深度接入 Home Assistant，并以标准设备和实体展示。
- **支持自然语言批量控制**：可用“打开所有灯”“关闭除了客厅以外的灯”等自然语言指令控制设备。
- **融入 Home Assistant 自动化**：小瑞 Agent 相关实体可在 Home Assistant 中查看、控制，并用于自动化规则。

## 二、版本兼容性

- 最低支持 Home Assistant `2025.10.0`。
- Release 版本号与 Home Assistant 年月大版本对应，请安装与当前 Home Assistant 年月大版本匹配的 Release。
- 例如：`2025.10.x` 版本适配 `HA 2025.10.x`，`2026.4.x` 版本适配 `HA 2026.4.x`。

## 三、安装指南

### 推荐方式：通过 HACS 安装

使用 HACS 安装更方便，也便于后续升级。安装前请确认 Home Assistant 已安装 [HACS（Home Assistant 社区商店）](https://hacs.xyz/)。

1. 打开 Home Assistant 后台，进入 **HACS**。
2. 切换到 **集成（Integrations）** 分类，搜索 **HOUZZkit AI**。
3. 打开搜索结果中的 [HOUZZkit AI](https://my.home-assistant.io/redirect/hacs_repository/?category=integration&owner=houzzkit&repository=houzzkit-ai-ha)。
4. 选择与当前 Home Assistant 年月大版本匹配的 Release 版本安装。
5. 安装完成后，重启 Home Assistant。

### 备选方式：手动安装

没有 HACS 时，可手动安装 Release 压缩包。

1. 访问 [HOUZZkit AI HA Releases](https://github.com/houzzkit/houzzkit-ai-ha/releases)，下载与当前 Home Assistant 年月大版本匹配的 Release 压缩包。
2. 解压后，将 `custom_components/houzzkit_ai` 文件夹复制到 Home Assistant 配置目录下的 `custom_components` 文件夹中。
3. 如果 Home Assistant 配置目录下没有 `custom_components` 文件夹，请先手动创建。
4. 重启 Home Assistant。

## 四、绑定与配置

安装并重启 Home Assistant 后，需要在 Home Assistant 中添加集成，并通过「小瑞 Agent 微信小程序」扫码绑定。

绑定前请确认：音箱、手机和 Home Assistant 在可互通的局域网内。

绑定步骤：

1. 进入 **设置 → 设备与服务 → 集成**。
2. 点击右下角 **+ 添加集成**，搜索并选择 **HOUZZkit AI**。
3. 使用「小瑞 Agent 微信小程序」扫描弹窗中的二维码。
4. 按页面提示确认绑定信息。

绑定成功后，可在 **设置 → 设备与服务** 中看到 HOUZZkit AI 集成，并在设备或实体列表中查看小瑞 Agent 相关实体。

## 五、使用前准备

小瑞 Agent 只会查询或控制 Home Assistant 中已公开给语音助手的实体。使用前建议先整理语音助手可见的实体：

- 给需要控制或查询的实体添加中文别名，建议使用清晰、常用、自然的叫法。
- 移除不需要语音控制的实体，减少识别和匹配干扰。
- 别名里不要带区域名称，区域交给 Home Assistant 的房间/区域信息表达。例如，建议使用“主灯”“筒灯”“空调”，不要使用“客厅主灯”“卧室空调”。

## 六、设备控制能力

小瑞 Agent 可通过 MCP 控制 Home Assistant 中已公开给语音助手的设备。具体能查询、开关、调节或设置哪些内容，以设备在 Home Assistant 中实际支持的能力为准。

### 支持的设备类型

灯、开关、按钮、窗帘、风扇、空调、加湿器、传感器。

### 可执行的操作

- 查询设备信息和状态
- 打开或关闭设备
- 调节设备数值
- 设置设备运行模式

### 自然语言示例

- 打开所有灯
- 打开除了客厅以外的灯
- 把灯调成红色
- 把空调调到 26 度
- 把窗帘打开一半

### 可调节的内容

- 灯亮度、灯色温、灯光颜色
- 音箱音量、音箱屏幕亮度
- 空调温度、空调风速
- 加湿器湿度
- 风扇风速
- 窗帘打开幅度

### 可设置的运行模式

- 空调：制热、除湿、自动、制冷、通风
- 加湿器：自动、睡眠

### 限制说明

- 未公开给 Home Assistant 语音助手的实体不会被小瑞 Agent 查询或控制。
- 不同设备支持的能力不同，实际可用操作以 Home Assistant 中该设备的实体能力为准。
- 如果一个设备没有亮度、颜色、风速或模式等能力，对应调节指令不会生效。

## 七、常见问题

### 应该安装哪个版本？

请安装与当前 Home Assistant 年月大版本匹配的 Release。例如，`HA 2025.10.x` 对应 `2025.10.x` Release。

### HACS 中搜索不到 HOUZZkit AI 怎么办？

请先确认 HACS 已正常安装，并刷新 HACS 仓库数据。如果仍然搜索不到，可从 [HOUZZkit AI HA Releases](https://github.com/houzzkit/houzzkit-ai-ha/releases) 下载对应版本并手动安装。

### 添加集成时搜不到 HOUZZkit AI 怎么办？

请确认已按步骤安装到 `custom_components/houzzkit_ai`，并且安装后已经重启 Home Assistant。如果仍然搜不到，请检查安装版本是否与当前 Home Assistant 年月大版本匹配。

### 扫码绑定失败怎么办？

请检查小瑞 Agent 设备、手机和 Home Assistant 是否在可互通的局域网内，并确认手机可以访问绑定页面显示的 Home Assistant 局域网 IP 地址。

### 绑定页面打不开或二维码无法加载怎么办？

通常是网络访问问题。请确认 Home Assistant 的局域网地址可以被手机访问，并检查 Home Assistant 的网络配置、反向代理、防火墙或跨网段访问设置。

### 绑定成功后语音无法控制设备怎么办？

请进入 Home Assistant 语音助手相关设置，确认目标实体已公开给语音助手，并设置了清晰的中文别名。也请检查设备本身是否支持对应操作，例如亮度、颜色、温度、风速或运行模式。
