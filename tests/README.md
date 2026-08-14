# tests 目录说明

自动化测试按模块组织，每个测试文件对应 `doc/design/` 中的模块或一类流程。

| 文件 | 覆盖 | 对应文档 |
|------|------|----------|
| test_subject_process.py | 主体核心闭环：认识状态迁移、承诺、关切生命周期 | doc/design/06、07 |
| test_external_activity_flow.py | 外部活动七阶段流程（阶段 0–6） | doc/design/00 |
| test_personal_world_port.py | 个人世界装载（importance、预算、常驻约束） | doc/design/08 |
| test_active_zone.py | 02 活跃区：空活跃区初始化、预算/弹性、剔除、显式召回、模型聚焦回填 | doc/design/02 |
| test_new_flow.py | 新流程系统占位：活跃区装载、追加召回、反馈/治理钩子、preview | doc/design/02、05、10、11 |
| test_recognition_profile.py | 01 身份识别：对象档案、必填校验、门禁、落库、认知确认 | doc/design/01 |

运行：

```powershell
conda run -n py3125 python -m pytest -q                                        # 全套
conda run -n py3125 python -m pytest tests/test_recognition_profile.py -q      # 单个模块
```

`smoke/` 存放可查看的冒烟测试数据库（见 [smoke/README.md](smoke/README.md)）。`idiot/` 为《白痴》场景测试树，当前搁置。
