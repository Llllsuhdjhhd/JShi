# Qwen3-ASR 1.7B Windows 对接指南

> 本文档供 Windows 侧项目参考：ASR 推理跑在 WSL2 Ubuntu，Windows 负责采集 / VAD / 声纹等前处理，再把音频片段发给本机服务。

日常启动语音＋图像、选择`stone`主体和切换模型，请先看《[启动指南](启动指南.md)》；本页侧重ASR接口与部署细节。

---

当前策略（2026-10-22 10:37，北京时间）：Windows默认500ms/3.5秒切段，Smart Turn CPU整句检测与千问batch并行；未完片段按声音组接续，放行后交JEV1。首次准备增加`.[voice-turn]`和`voice-setup --turn`，详见《[启动指南](启动指南.md#8-切段与整句策略切换)》。

## 匠石当前接入：原生batch（2026-10-21 09:31，北京时间）

本地Qwen3-ASR作为`qwen`后端，原火山`online`、Deepgram与CPU`local`继续保留。模块边界见《[语音模块架构](语音模块架构.md)》。当前改为：同一处理窗内的独立片段组成一个batch，一次HTTP提交，服务端一次原生`model.transcribe(audio=[...])`，返回每段自己的文字。没有客户端并发，也没有服务端循环发HTTP模拟batch。

### 准备与启动

先在项目Windows环境准备前处理模型：

```powershell
python -m pip install -e ".[voice-local]"
.\jshi.cmd voice-setup --vad --diarize
```

再启动项目的新WSL批量服务（替代旧`qwen-asr-serve`，不要同时占用同一端口和GPU）：

```powershell
wsl -d Ubuntu -- bash /mnt/c/Users/Lenovo/Desktop/project/JShi/tools/start_qwen_batch_wsl.sh
```

该脚本复用现有WSL环境和1.7B权重，不另下模型。原生离线引擎的预填充预算设置为2048 tokens、每个样本限一条音频，适配本机8GB显卡；这不改变每批多条独立样本的语义。看到`Qwen batch service ready`后再启动Windows：

```powershell
.\voice.cmd --asr qwen --tts local
```

可选`.env`配置：

```dotenv
JSHI_VOICE_ASR=qwen
JSHI_VOICE_TTS=local
JSHI_QWEN_ASR_URL=http://127.0.0.1:8000/v1
JSHI_QWEN_ASR_MODEL=
JSHI_QWEN_ASR_TIMEOUT_S=60
JSHI_QWEN_ASR_BATCH_SIZE=8
JSHI_VOICE_SPEAKER_MODEL=cam++
```

模型名留空时，从`/v1/models`发现唯一模型，同时要求`capabilities.audio_batch=true`。普通`qwen-asr-serve`没有此接口，会明确提示改用批量服务；不会自动改回并发请求或云端。旧`JSHI_QWEN_ASR_CONCURRENCY`已移除，改配批次大小。

客户端和服务端默认每批最多8段；客户端取双方上限的较小值，超过后分成小批依次处理。WSL服务端上限由启动前环境变量`JSHI_QWEN_BATCH_SIZE`调整；Windows客户端上限用`JSHI_QWEN_ASR_BATCH_SIZE`。显存压力大时可减小，例如4；不是把多人音频拼成一条音频。正常采集不额外等待凑满8段，已有一个处理窗就提交，单段也是batch=1。

### 当前请求全文与关联规则

`POST /v1/asr/batch`。每段为16kHz单声道16位PCM封装的WAV，`s1/s2`仅在本次HTTP请求内有效；人物ID、声纹向量、绝对时间和原始记录不发送。

```json
{
  "model": "<服务返回的模型名称>",
  "items": [
    {"id": "s1", "audio_url": "data:audio/wav;base64,<第一段WAV>"},
    {"id": "s2", "audio_url": "data:audio/wav;base64,<第二段WAV>"}
  ]
}
```

服务一次调用：

```python
results = model.transcribe(
    audio=[(audio1_float32, 16000), (audio2_float32, 16000)],
    language=None,
    return_time_stamps=False,
)
```

已核对本机安装的Qwen SDK：每条音频的内部完整材料为`system=""`、`user=[{"type":"audio","audio":该条音频}]`；context默认空，不强制语言，不加项目热词。SDK生成各自的音频prompt后，将输入列表一次传给vLLM的`generate(batch, ...)`。批次是多条独立样本一起推理，不是一个prompt里放多个人声。

响应为`{"model":"...","results":[{"id":"s1","text":"...","language":"Chinese"}, ...]}`。Windows按短代号映射回原片段，并校验数量、重复、遗漏和未知代号；整个批次有效后才交付。分离得到的各条音轨也走这一批接口。未分离的重叠音频不会因为batch自动变成各人的独立声音。

### 实测记录与版本边界

当前批量服务已用两段官方公开音频实测：s1为4.204秒中文、s2为15.051秒英文，一个HTTP请求返回两条独立结果；中文为“甚至出现交易几乎停滞的情况。”，英文结果完整保存在报告中。首次双段batch耗时6.678秒；随后中文通过采集/VAD/声纹分段/原生batch完整链路约0.567秒，均为本次单次测量，不代表多人实时延迟或通用性能。实测记录见`work/qwen-batch-live-report.json`。模块测试另验证三条音频只触发一个HTTP请求与一次原生批推理，中文家庭多人实际准确率仍需真实录音验证。

历史单条请求版本（2026-10-21 09:14）：官方公开`asr_zh.wav`通过直接转写和完整前处理链，均返回“甚至出现交易几乎停滞的情况。”，测量约0.605秒、0.653秒；完整记录为`work/qwen-live-smoke/report.json`。这两个耗时不是当前batch版本测量，不用于推断多人batch性能。

---

**以下第1～9节保留原标准`qwen-asr-serve`的部署与联调参考；其中`/v1/chat/completions`及原启动脚本不适用于当前项目batch模式。当前接入以本节为准。**

## 1. 当前部署概况

| 项目 | 路径 / 说明 |
|------|-------------|
| 机器 | LAPTOP-L3PSRCJE，RTX 5060 Laptop 8GB |
| 运行环境 | WSL2 + Ubuntu，用户 `lenovo` |
| Conda | `/home/lenovo/miniconda3` |
| 独立环境 | `qwen3-asr`（Python 3.12） |
| 模型 | `/home/lenovo/qwen3-asr/models/Qwen3-ASR-1.7B`（约 4.4GB） |
| 启动脚本 | `/home/lenovo/qwen3-asr/start_server.sh` |
| 服务地址 | `http://127.0.0.1:8000`（Windows 与 WSL 互通） |
| 启动参数 | `--gpu-memory-utilization 0.85 --max-model-len 8192` |

说明：WSL 内存上限约 12GB（`.wslconfig`），是上限不是一开机占满。

---

## 2. 推荐架构（家庭对话机器人）

```
麦克风
  → Windows：采集 PCM（建议 16kHz / 单声道 / 16bit）
  → Windows：VAD 切有效人声（如 Silero VAD）
  → Windows：声纹 + 增量聚类 → speaker_id（可选）
  → HTTP：把音频片段发给 WSL 的 Qwen3-ASR
  → Windows：把返回文本与 speaker_id 绑定展示 / 送下游 LLM
```

要点：

1. **不要把声纹向量发给 ASR**。ASR 只吃音频，出文本。
2. 多人轮流说话：切一段识别一段；多段离线可 batch（本服务也支持并发请求）。
3. 两人同时叠话时，简单切段会变难，需另做重叠处理。

---

## 3. 启动 / 停止服务

### 3.1 在 WSL 里启动（推荐）

打开 Ubuntu 终端，执行：

```bash
bash /home/lenovo/qwen3-asr/start_server.sh
```

看到类似下面日志才算就绪：

- `Application startup complete`
- 或 `Uvicorn running on http://0.0.0.0:8000`

首次启动可能要 1～3 分钟（加载模型 + 编译）。

### 3.2 从 Windows PowerShell 启动

```powershell
wsl -d Ubuntu -- bash /home/lenovo/qwen3-asr/start_server.sh
```

### 3.3 停止

在跑服务的终端按 `Ctrl+C`。或：

```powershell
wsl -d Ubuntu -- bash -lc "pkill -f qwen-asr-serve || true"
```

### 3.4 健康检查

浏览器或 PowerShell：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/v1/models
```

能返回模型列表即表示服务在线。

---

## 4. API 调用方式

服务兼容 OpenAI Chat Completions 风格，路径：

`POST http://127.0.0.1:8000/v1/chat/completions`

Header：`Content-Type: application/json`

### 4.1 用公网音频 URL（联调）

```json
{
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "audio_url",
          "audio_url": {
            "url": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-ASR-Repo/asr_zh.wav"
          }
        }
      ]
    }
  ]
}
```

实测返回示例（摘要）：

```text
language Chinese<asr_text>甚至出现交易几乎停滞的情况。
```

可用 `qwen_asr.parse_asr_output(content)` 拆成 `(language, text)`；Windows 侧也可自己按 `<asr_text>` 分割。

### 4.2 用本地文件（推荐生产）

把本地音频封装为 WAV 后转成 **data URL（base64）** 放进 `audio_url.url`：

```text
data:audio/wav;base64,AAAA...
```

也可用服务端能读到的路径（若你把文件放到 WSL 可见目录）。家庭机器人更稳妥的是 **Windows 读文件 → base64 data URL → POST**。

### 4.3 Python 示例（Windows 或 WSL 均可）

```python
import base64
import requests
from pathlib import Path

API = "http://127.0.0.1:8000/v1/chat/completions"

def asr_file(path: str, language_hint: str | None = None) -> str:
    raw = Path(path).read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    # 按实际格式改 mime：wav / mpeg / flac 等
    data_url = f"data:audio/wav;base64,{b64}"
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "audio_url", "audio_url": {"url": data_url}}
                ],
            }
        ]
    }
    r = requests.post(API, json=payload, timeout=300)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    # 解析 language / text
    if "<asr_text>" in content:
        lang, text = content.split("<asr_text>", 1)
        lang = lang.replace("language", "").strip()
        return text.strip()
    return content.strip()

if __name__ == "__main__":
    print(asr_file(r"D:\path\to\utterance.wav"))
```

### 4.4 PowerShell 快速联调（URL 样例）

```powershell
$body = @{
  messages = @(
    @{
      role = "user"
      content = @(
        @{
          type = "audio_url"
          audio_url = @{
            url = "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-ASR-Repo/asr_zh.wav"
          }
        }
      )
    }
  )
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/chat/completions" `
  -Method Post -ContentType "application/json" -Body $body
```

---

## 5. Windows 侧音频约定

为减少转码开销，建议全链路统一：

- 采样率：`16000 Hz`
- 声道：单声道
- 采样格式：`PCM 16-bit`
- 容器：`WAV`。当前适配器会补齐WAV头；不能将raw PCM直接标成audio/wav发送。

VAD 切出的每段有效人声单独请求一次 ASR；不要等整段长对话结束再识别。

---

## 6. 延迟与性能预期

- 模型常驻 GPU 后，短句识别通常可到亚秒～约 1～2 秒量级（含网络与切段）。
- 冷启动（刚开服务）更慢，产品侧建议：**开机后预热服务**，并做一次 dummy 请求。
- 官方 vLLM 单并发 TTFT 约百毫秒量级；家庭本机还会叠加 WSL / 切段 / HTTP 开销。
- 显存：权重约 4GB 级；当前参数已按 8GB 卡调过。若再 OOM，可把 `--max-model-len` 降到 `4096`。

---

## 7. 常见问题

### 7.1 `127.0.0.1:8000` 连不上

1. 确认 WSL 里服务已启动且日志有 `Application startup complete`
2. 确认没有被防火墙拦本机回环（一般不会）
3. 用 `wsl hostname -I` 看 WSL IP；多数情况 `127.0.0.1` 即可

### 7.2 启动报 KV cache / max seq len 不够

降低 `--max-model-len`，或略提高 `--gpu-memory-utilization`（注意别把系统挤爆）。当前脚本已是 `8192` / `0.85`。

### 7.3 启动报找不到 C compiler

环境里已装 conda 的 `gcc/g++`，启动脚本会设置：

```bash
export CC=/home/lenovo/miniconda3/envs/qwen3-asr/bin/gcc
export CXX=/home/lenovo/miniconda3/envs/qwen3-asr/bin/g++
```

请用 `start_server.sh` 启动，不要裸跑缺环境变量的命令。

### 7.4 tokenizer 警告

日志里可能出现 `incorrect regex pattern` / `fix_mistral_regex` 提示，实测识别可用，可先忽略。

### 7.5 卸载 / 清空间

```bash
source ~/miniconda3/etc/profile.d/conda.sh
conda deactivate
conda env remove -n qwen3-asr
rm -rf ~/qwen3-asr ~/miniconda3   # 若整个 Miniconda 也不要了再删
```

或在 Windows：`wsl --unregister Ubuntu`（会删掉整个 Ubuntu，慎用）。

---

## 8. 与 Windows 项目的最小对接清单

1. 进程启动时检测 `http://127.0.0.1:8000/v1/models`，失败则提示用户先开 WSL 服务，或由项目自动 `wsl -d Ubuntu -- bash /home/lenovo/qwen3-asr/start_server.sh`
2. VAD 切段 → 存临时 `wav`
3. `POST /v1/chat/completions`（base64 data URL）
4. 解析 `<asr_text>` 后的文本
5. （可选）附带 `speaker_id` 写入你自己的会话结构
6. 临时文件用完删除

---

## 9. 相关本地文件

| 用途 | 位置 |
|------|------|
| 启动脚本 | WSL: `/home/lenovo/qwen3-asr/start_server.sh` |
| 模型目录 | WSL: `/home/lenovo/qwen3-asr/models/Qwen3-ASR-1.7B` |
| 冒烟脚本 | WSL: `/home/lenovo/qwen3-asr/smoke_test.py` |
| 接口测试 | WSL: `/home/lenovo/qwen3-asr/api_test.py` |
| 本文档 | 项目内：`doc/Windows对接指南.md` |

---

*文档生成日期：2026-10-21（Asia/Shanghai）。部署以本机实际路径为准。*
