# Windows 本机启动与 CLI 测试

本文说明如何在 Windows 上启动匠石，并确认真实模型和 Jshi_memory 正常接入。项目启动脚本会自动定位仓库、加载项目 `.env`，数据目录固定为仓库根目录的 `.jshi`。

## 1. 第一次检查配置

确保仓库根目录有本机 `.env`，至少配置：

```dotenv
JSHI_MODEL_ENDPOINT=你的模型服务地址
JSHI_MODEL_API_KEY=你的密钥
JSHI_MODEL_NAME=你的模型名
JSHI_MEMORY_BACKEND=rems3
JSHI_REMS_SKIP_EMBEDDING=
```

向量模型已经缓存在本机时，`HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1` 可以保留。不要把 API 密钥发到聊天或提交进 Git。

## 2. 启动对话

在 PowerShell 里可以从任意目录调用脚本。把路径改成这份仓库的实际位置：

```powershell
& 'C:\Users\Lenovo\Desktop\project\JShi\talk.cmd' stone --speaker lux
```

如果当前目录已经是 JShi 仓库根目录，也可以简写：

```powershell
.\talk.cmd stone --speaker lux
```

看到下面这行，说明这次进程选择了 Jshi_memory：

```text
[jshi] memory backend: Rems3MemoryBackend
```

输入 `/help` 查看对话命令，输入 `/quit` 结束。`--speaker` 使用 `.jshi` 里已有的对象档案名；`lux` 是示例，按自己的档案名替换。

## 3. 先直接检索，确认记忆端返回内容

对话前可以用 CLI 查询一条已确认存入长期记忆的独特事实：

```powershell
& 'C:\Users\Lenovo\Desktop\project\JShi\jshi.cmd' recall stone '这里换成记忆中的关键词' --limit 5
```

这条命令不调用对话模型，会打印命中的事件编号和原文。可以加 `--object-id 对象ID` 限定一个对象；不加则跨对象检索。CLI 当前的 `recall` 命令只提供单个对象筛选参数。

确认返回片段包含目标事实后，再运行 `talk.cmd`，询问同一件事，并对照直接检索结果判断回答是否有记忆依据。只看回答正确与否不足以证明长期记忆起了作用。

## 4. 常见问题

- **`No module named 'jshi'`**：直接运行 Python 时没有安装源码包或没有设置 `PYTHONPATH`。优先使用仓库提供的 `talk.cmd` / `jshi.cmd`；它们会自动设置源码路径。
- **提示使用 `InProcessMemoryBackend`**：检查 `.env` 中 `JSHI_MEMORY_BACKEND=rems3`，并确认邻仓 `Jshi_memory/src` 可用。
- **Hugging Face 离线模型加载失败**：检查 BAAI 向量模型权重是否在当前 Windows 用户的 Hugging Face 缓存中；不要设置 `JSHI_REMS_SKIP_EMBEDDING=1`。
- **切错数据目录**：启动脚本明确使用仓库根目录 `.jshi`。不要从另一份仓库或另一个数据目录启动。

## 5. 脚本选择解释器的规则

`jshi.cmd` 和 `talk.cmd` 按以下优先级选择 Python：环境变量 `JSHI_PYTHON`、当前已激活 Conda 环境、`%USERPROFILE%\miniconda3\python.exe`、PATH 中的 `python`。本机实测使用的是 Miniconda base；切换到其他环境时，请先确认该环境也能导入 Jshi_memory 依赖。
