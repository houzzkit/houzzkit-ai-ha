# 仓库指南

该仓库是 Home Assistant 自定义集成，服务微信小程序与「小瑞 Agent」智能音箱链路。

## 协作与语言

- 与本仓库相关的回复、说明和生成文档默认使用中文，除非代码、命令、标识符或引用原文本身需要保留英文。
- 用户使用 `review` 命令时，回复必须使用中文，并且要明确列出发现的问题；如果没有发现问题，也要说明未发现明确问题。
- 修改前先检查工作区状态；不要覆盖或回退用户已有改动。
- 变更应保持聚焦，优先沿用 Home Assistant 集成的既有结构和命名。
- Markdown 文档遵守常见 markdownlint 格式：标题和列表前后留空行，代码块标注语言。

## 项目结构

本仓库是 HOUZZkit AI 的 Home Assistant 自定义集成，用于连接「小瑞 Agent」智能音箱与 Home Assistant。

- `custom_components/houzzkit_ai/`：集成主体代码。
- `custom_components/houzzkit_ai/manifest.json`：Home Assistant 集成元数据、依赖和版本。
- `custom_components/houzzkit_ai/services.yaml`：集成服务声明。
- `custom_components/houzzkit_ai/translations/`：英文与简体中文翻译。
- `custom_components/houzzkit_ai/houzzkit/`：与 HOUZZkit 后端、音频、HTTP、WebSocket、MCP、STT/TTS 传输相关的封装。
- `tests/`：聚焦测试，目前以标准库 `unittest` 为主。
- `hacs.json`：HACS 发布元数据。
- `.github/workflows/`：HACS、hassfest 校验和 release zip 工作流。

## 开发与验证命令

仓库没有独立 `pyproject.toml`，本地通常使用已准备好的 Home Assistant 虚拟环境。默认以 Home Assistant 2025.10 作为开发和验证版本；虚拟环境目录名不作固定要求。当前工作区可用示例：

```bash
source .venv_homeassistant-2025.10/bin/activate
python -m unittest discover -s tests
```

运行单个测试文件：

```bash
python -m unittest tests.intent_automation_protocol_test
```

提交前至少运行与变更相关的测试。涉及集成元数据、依赖、HACS 发布结构或 Home Assistant 平台声明时，还要考虑 GitHub Actions 中的 HACS validation 与 hassfest 约束。

## 代码风格

- Python 代码保持现有风格：4 空格缩进、类型标注、异步 Home Assistant API 约定。
- Home Assistant 平台文件命名遵循官方平台名，例如 `sensor.py`、`switch.py`、`media_player.py`。
- 新增实体、服务、配置项时同步检查翻译、诊断、修复提示和 manifest 依赖是否需要更新。
- 不要在集成初始化路径引入重型副作用；测试中会绕开 `__init__` 直接加载部分模块。
- 保持用户可见文本中英文翻译一致，避免只更新 `zh-Hans.json` 或只更新 `en.json`。

## 测试指南

- 测试文件放在 `tests/`，命名建议为 `*_test.py`。
- 对意图解析、自动化计划、时间处理、实体匹配、协议兼容这类高风险逻辑补充聚焦单元测试。
- 时间相关测试应固定 `dt_util.now()` 和默认时区，避免依赖运行机器的当前时间。
- 尽量用轻量 mock 隔离 Home Assistant 运行时，避免为了测试纯转换逻辑启动完整 HA。

## Home Assistant 集成注意事项

- `DOMAIN` 必须保持为 `houzzkit_ai`，并与目录名、manifest、HACS 包名一致。
- 修改 `manifest.json` 的 `requirements`、`dependencies`、`after_dependencies` 时确认导入路径和运行时依赖匹配。
- 新增平台文件时确认 `async_setup_entry`、实体唯一 ID、设备信息和卸载路径符合 Home Assistant 约定。
- 处理蓝牙、ESPHome、音频、FFmpeg、MCP、STT/TTS 等路径时，注意异步取消、断线重连和日志级别，不要泄露 token、密钥或用户家庭数据。

## 发布与提交

- Release 工作流会打包 `custom_components/<domain>/` 为 zip；不要把无关临时文件放进集成目录。
- HACS 元数据在 `hacs.json`，集成版本在 `custom_components/houzzkit_ai/manifest.json`。
- 近期提交信息既有英文也有中文，建议使用简短、明确的祈使句或 `feat:`、`fix:` 前缀。
- PR 或交付说明应列出用户可见变化、已运行测试命令，以及是否修改了翻译、manifest、HACS 或 release 相关文件。
