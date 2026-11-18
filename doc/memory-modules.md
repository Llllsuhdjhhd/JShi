# 按启动参数选择记忆模块

JShi 通过原有 MemoryBackendPort 调用记忆模块。启动参数只选择实现与配置，不改变主认知接口。
在 JShi 目录运行：

```powershell
.\jshi.cmd memory-modules
.\jshi.cmd vision stone --memory-module dual
.\jshi.cmd vision stone --memory-module legacy
.\jshi.cmd vision stone --memory-module inprocess
```

先正常退出已运行的 JShi，再执行另一条启动命令。这是启动时选择，不是运行中热切换。
原有 ASR、TTS 等参数照常追加；`--memory-module` 也可放在子命令前。

当前本机配置在 `.jshi/memory-modules.json`，默认 `dual`：

| 名称 | 实现 | 独立数据目录（相对 `.jshi`） |
|---|---|---|
| dual | JShi_memory_old_days：长期 L1 + 原生 Mem0 并行；L0 可回查 | rems-dual-20261117 |
| legacy | 原 JShi_memory 与原配置 | rems |
| inprocess | JShi 自带简单记忆 | memory-inprocess |

`dual` 内部的长期库与 Mem0 近期库也彼此独立。选择 `dual` 仍然同时检索两库，不会变成二选一。
`inprocess` 是简单后端，首次使用会按未消费进度回放，不代表预先拥有另外两个库的数据。

参数优先于 `JSHI_MEMORY_MODULE` 环境变量，再优先于 JSON 的 `default`。
没有配置文件且没有指定模块时，兼容原来的 `JSHI_MEMORY_BACKEND` 等环境变量。
可通过 `--memory-modules-file <路径>` 指定别的注册表；相对代码与数据路径均以该 JSON 所在目录为基准。
模块内 `settings` 仅接受 `REMS_` 配置，JShi 的模型凭据仍使用已有环境配置。

## 切换与数据进度

每个模块的代码根目录、数据库目录和记忆消费游标分别绑定。重新切回某模块，
从它自己的游标续接经历，不借用另一个模块已经处理到的位置。
游标身份包含模块名、后端、代码根目录与存储目录；修改路径会形成新的进度身份。
不同模块的数据目录不能相同或互相包含；同一份 JShi 台账不允许同时启动多个模块实例。

配置中的 `bootstrap_cursor` 只在该进度身份首次初始化时生效：

- 不配置：从 0 开始，回放仍保留的历史经历。
- 非负整数，或 `{ "主体ID": 序号 }`：用于已核实的既有库，未列出的主体从 0 开始。
- `"current"`：继承切换前的共享记忆游标；只能用于已完成相应数据导入的库。

本机 `dual` 已完成导入，所以继承原游标；`legacy` 的初始位置来自切换前核实的原库记录。
初始化后，配置中的种子不会覆盖已保存的独立进度。台账裁剪同时考虑已登记模块的游标，
因此暂未启用的旧模块可能让原始台账保留更久。这不计入 Mem0 自身的 10 GB 目录预算。
切换参数不会复制、删除或合并两个模块的数据库。

新增 REMS 兼容模块可参照 `memory-modules.example.json`，配置独立 `source_root`、`data_dir` 与参数。
不同协议的模块需要先实现 JShi 后端适配器；不能仅填一个名称便自动兼容。
启动环境由 `JSHI_PYTHON` 或本机 `.jshi/python-path.txt` 选择，必须具备所选模块的依赖。

本次没有加入 JShi 按问题选择搜索范围/深度的策略；该事项按约定后续再做。
