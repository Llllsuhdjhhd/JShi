# runners

装载书本材料、执行用例、写出详细报告的脚本放这里。

## 拟议命令（待实现）

```text
python -m tests.idiot.runners.run_case --case cases/scenes/T1/idiot-T1-ex-01.md
```

职责：

1. 创建独立 data-dir（如 `.jshi-idiot/<case-id>/`）
2. 读 preset JSON，create + 写入个人内容
3. 读 pack，跳过 `#` 行，装载书中对白
4. 读 probe，调用 preview-state / experience 等
5. 按方案第 8 节写 `reports/<run-id>/<case-id>.md`
6. 污染检查：不得改写 packs/presets/corpus

在专用命令齐备前，可按用例 md 手工执行，仍须产出同等详细报告。
