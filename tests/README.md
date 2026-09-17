# tests 目录说明

自动化测试按模块组织，每个测试文件对应 `doc/design/` 中的模块或一类流程。

| 文件 | 覆盖 | 对应文档 |
|------|------|----------|
| test_subject_process.py | 主体核心闭环：认识状态迁移、承诺、关切生命周期 | doc/design/06、07 |
| test_external_activity_flow.py | 外部活动七阶段流程（阶段 0–6） | doc/design/00 |
| test_personal_world_port.py | 个人世界装载（importance、预算、常驻约束） | doc/design/08 |
| test_values.py | 100 价值观与边界（生命周期、装载、导入导出、自省） | doc/design/100 |
| test_active_zone.py | 02 活跃区：空活跃区初始化、预算/弹性、剔除、显式召回、模型聚焦回填 | doc/design/02 |
| test_new_flow.py | 新流程系统占位：活跃区装载、追加召回、反馈/治理钩子、preview | doc/design/02、05、10、11 |
| test_recognition_profile.py | 01 身份识别：对象档案、必填校验、门禁、落库、认知确认 | doc/design/01 |

运行（**单测用 Anaconda base，不要用 `py3125`，也不要用 PATH 上的 base `python`**）：

```powershell
$env:PYTHONPATH="src"
& "C:\Users\40575\anaconda3\python.exe" -m pytest -q -p no:cacheprovider --basetemp .tmp/pytest/tmp            # 全套
& "C:\Users\40575\anaconda3\python.exe" -m pytest tests/test_recognition_profile.py -q -p no:cacheprovider --basetemp .tmp/pytest/one   # 单个模块
```

为什么指定解释器：Anaconda base 是 3.11.7 + pytest 9.0.3，`tmp_path` 用例能真跑、`N passed` 汇总完整。
PATH 上的 `python`（常见 base 3.14）在这台机器上跑 `tmp_path` 用例会因 basetemp 清理缺陷假报 `E`
（`PermissionError: [WinError 5]`），**据此改代码会改错方向**。conda `py3125` 只用于对话 / 记忆实测（`talk.cmd`），
不用来跑单测。完整说明见 `.cursor/rules/环境与工具陷阱.mdc` §3 与 `doc/协作流程.md` §6。

缓存与临时文件统一在仓库根下的 `.tmp/`（已忽略）：pytest 用 `.tmp/pytest/`，探测报告用 `.tmp/live-loop/`，临时 data-dir 用 `.tmp/data/<名>/`。不要写进 `tests/`。

`smoke/` 存放可查看的冒烟测试数据库（见 [smoke/README.md](smoke/README.md)）。`idiot/` 为《白痴》场景测试树，当前搁置。
