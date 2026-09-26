# 匠石（JShi）

JShi 是一个长期陪伴型 AI 的主体过程实验。项目关注主体记录、个人世界、上下文、记忆与回应如何跨活动延续。

本文档按当前代码整理。模块地图保持粗略，具体模块的行为以实现和测试为准。

## 从这里开始

- [项目脉络与模块地图](doc/项目脉络.md)
- [文档入口](doc/README.md)
- [模块实施草稿箱](doc/草稿箱/README.md)
- [旧版设计与方案归档](doc/历史文档/README.md)

## 运行

需要 Python 3.11 或更新版本。安装开发依赖并查看命令：

    python -m pip install -e ".[dev]"
    jshi --help

CLI 配置示例见仓库根目录的 .env.example。默认数据目录是 .jshi，可通过 CLI 参数或 JSHI_DATA_DIR 指定其他目录。CLI 默认使用 Pi 工具引擎；离线测试和演示可设置 JSHI_TOOL_ENGINE=stub。

运行自动化测试：

    python -m pytest -q

## 当前边界

项目仍处于实验阶段。模块接口可能调整；部分行动、反馈和治理能力仍是占位实现。当前代码可以验证主流程和数据结构，跨活动行为结论需要单独设计对照实验。

## License

MIT，见 [LICENSE](LICENSE)。
