# 测试入口

自动化测试使用 pytest，默认从 src 加载项目代码：

    python -m pip install -e ".[dev]"
    python -m pytest -q

tests/test_*.py 是离线自动化测试。REMS3 相关 live 测试默认跳过；需要显式设置 JSHI_TEST_REMS=1，并先确认本机邻仓与数据配置。

tests/live/ 下的脚本可能调用真实模型或记忆后端，部分脚本会写入持久化数据。运行前先查看各脚本的帮助和参数，明确数据目录及费用影响。

tests/smoke/ 保存可查看的冒烟样例。tests/idiot/ 保存文学场景测试材料与人工报告格式，属于专项测试素材，不代表通用运行流程。
