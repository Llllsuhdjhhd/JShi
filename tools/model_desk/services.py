"""Windows Model Desk uses the same Python environment and project as jshi.cmd."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener

JSHI = Path(__file__).resolve().parents[2]
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Model Desk" / "logs"
PHONE_STATE = LOG_DIR / "jshi-launch.json"
PHONE_PORT = 8767
PORTS = {"jshi-app": 8766, "webui": 3000}
LOCK = threading.Lock()
processes = {}
http = build_opener(ProxyHandler({}))
CHAT = "mlx-community/Qwen3.6-35B-A3B-4bit"
VISION_MODELS = {
    "deepseek": ("deepseek-v4-flash", "deepseek-v4-pro"),
    "openai": ("gpt-4o",),
    "gemini": ("gemini-3-flash-preview", "gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.8-flash"),
    "qwen": ("qwen3.5-flash", "qwen3.5-flash-2026-02-23"),
    "cloudflare": ("@cf/meta/llama-4-scout-17b-16e-instruct", "@cf/mistralai/mistral-small-3.1-24b-instruct"),
    "openrouter": ("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",),
}


def lan_addresses():
    # ConvertTo-Json also preserves non-ASCII interface names regardless of the console code page.
    command = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
               "Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred | "
               "Select-Object IPAddress,InterfaceAlias | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, timeout=8, check=True)
        rows = json.loads(result.stdout.decode("utf-8-sig"))
        if isinstance(rows, dict):
            rows = [rows]
        found = {row["IPAddress"]:row["InterfaceAlias"] for row in rows or []
                 if usable_address(row["IPAddress"])}
        if found:
            return found
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        return {ip:"局域网" for ip in socket.gethostbyname_ex(socket.gethostname())[2] if usable_address(ip)}
    except OSError:
        return {}


def usable_address(value):
    ip = ipaddress.IPv4Address(value)
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


def listener(port):
    result = subprocess.run(["netstat.exe", "-ano", "-p", "TCP"], capture_output=True, timeout=8)
    if result.returncode:
        raise ValueError("无法读取 Windows 监听端口")
    for line in result.stdout.splitlines():
        row = line.split()
        if (len(row)==5 and row[0]==b"TCP" and row[1].rsplit(b":",1)[-1]==str(port).encode()
                and row[2] in (b"0.0.0.0:0", b"[::]:0") and row[-1].isdigit()):
            return int(row[-1])
    return None


def launch_state(pid):
    if pid and PHONE_STATE.is_file():
        state = json.loads(PHONE_STATE.read_text(encoding="utf-8"))
        if pid == state.get("pid"):
            return state
    return {}


def phone_state(pid):
    state = launch_state(pid)
    return state if state.get("ip") and listener(PHONE_PORT)==pid else {}


def phone_details(state):
    url = f"https://{state['ip']}:{PHONE_PORT}/"
    with (LOG_DIR/"jshi-app.log").open("rb") as log:
        log.seek(state["offset"])
        for line in log:
            match = re.search(r"手机设备入口 "+re.escape(url)+r"\s+配对码 (\d{8})",line.decode("utf-8",errors="replace"))
            if match:
                return {"url":url, "code":match.group(1)}
    raise ValueError("尚未收到手机配对码，请稍后再试。")


def status(key):
    path = "/status" if key=="jshi-app" else "/health"
    try:
        with http.open(f"http://127.0.0.1:{PORTS[key]}{path}", timeout=1) as response:
            data = json.load(response)
        ready = "voice_active" in data if key=="jshi-app" else data.get("status") is True
        return "运行中" if ready else "检查服务"
    except (OSError, ValueError):
        child = processes.get(key)
        if child:
            code = child.poll()
            if code is None:
                return "启动中"
            if code:
                return "启动失败"
        return "未启动"


def jshi_args(options, config, asr, tts):
    args = [sys.executable, "-m", "jshi.app.cli", "--data-dir", config["directory"]]
    if options.get("memory"):
        args += ["--memory-module", options["memory"]]
    args += [options.get("mode","vision"), options.get("subject","stone"), "--port", str(PORTS["jshi-app"]),
             "--asr", asr, "--tts", tts]
    if options.get("capture")=="phone":
        args += ["--phone", options["phoneIp"], "--phone-port", str(PHONE_PORT)]
    return args


def launch_options():
    # Read the CLI's effective dotenv values and catalog without opening its databases.
    script = """import json, os
from jshi.app.cli import _load_local_env
from jshi.app.model_settings import MODELS
from jshi.vision.config import VisionConfig, MLX_ENDPOINT
_load_local_env()
vision=VisionConfig.from_env()
print(json.dumps(dict(directory=os.getenv('JSHI_DATA_DIR','.jshi'),models=MODELS,
    visionEnabled=vision.enabled,visionDefault=vision.model,visionLocal=vision.local_mlx and vision.endpoint==MLX_ENDPOINT,
    asr=os.getenv('JSHI_VOICE_ASR','online'),tts=os.getenv('JSHI_VOICE_TTS','online'))))
"""
    result = subprocess.run([sys.executable,"-c",script],cwd=JSHI,
                            capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError("无法读取匠石配置，请在项目目录运行 model-desk.cmd --setup，再重新打开工作台。")
    config = json.loads(result.stdout)
    directory = JSHI / Path(config["directory"]).expanduser()
    def read(name, fallback):
        path = directory / name
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else fallback
    config["directory"] = str(directory)
    config["subjects"] = {p["subject_id"]:p.get("name",p["subject_id"]) for p in read("identities.json",[])}
    memory = read("memory-modules.json",{})
    config["memory"] = list(memory.get("modules",{}))
    config["defaultMemory"] = memory.get("default","")
    config["banks"] = {model:len(read(file,{}).get("entries",{})) for model,file in
                       [("cam++","voiceprints.json"),("eres2netv2","voiceprints-eres2netv2.json")]}
    saved = read("model_settings.local.json",{})
    global_profile = saved.get("global_profile") or {}
    config["current"] = next(iter(global_profile.get("choices",[])),{}).get("model","")
    vision_profile = saved.get("profiles",{}).get("vision", {} if config["visionLocal"] else global_profile)
    config["visionCurrent"] = next(iter(vision_profile.get("choices",[])),{}).get("model") or config["visionDefault"]
    config["visionModels"] = {service:[m for m in models if m in config["models"].get(service,())]
                              for service,models in VISION_MODELS.items()}
    config["phoneAddresses"] = lan_addresses()
    config["models"].pop("mlx", None)
    return config


def voice_choices(options, config):
    def backend(name, valid):
        value = options.get(name)
        if value is None:
            value = options.get("voice","project")
        if value == "project":
            value = config[name]
        if value not in valid:
            raise ValueError("未知的语音设置")
        return value
    return backend("asr",("mlx","online","bailian","deepgram","local","qwen")), backend("tts",("mlx","online","local"))


def apply_models(chat, vision, key):
    origin = f"http://127.0.0.1:{PORTS['jshi-app']}"
    with http.open(origin+"/model-settings",timeout=5) as response:
        settings = json.load(response)
    global_profile = settings.get("global_profile") or {}
    vision_row = settings["rows"].get("vision")
    if vision and not vision_row:
        raise ValueError("此匠石实例未启用图像识别，请选择语音＋图像入口或手机采集")
    if key and settings["services"]["openai"].get("endpoint") != "https://api.openai.com/v1":
        raise ValueError("GPT 密钥仅可用于 OpenAI 官方地址")

    def profile_for(choice, current):
        service, model = choice.split("|",1)
        provider = settings["services"].get(service,{})
        if model not in provider.get("models",[]):
            raise ValueError("当前匠石进程未加载所选模型："+model)
        if not provider.get("endpoint") or (service!="mlx" and not provider.get("has_key") and not (service=="openai" and key)):
            raise ValueError("请先在匠石的模型设置中配置 "+provider["label"]+" 的服务地址和 API Key")
        selected = {"service":service,"model":model}
        if current.get("choices")==[selected]:
            return current
        return {"choices":[selected],"effort":next(iter(provider["efforts"][model]))}

    chat_profile = profile_for(chat,global_profile) if chat else global_profile
    current_vision = (vision_row or {}).get("profile") or {}
    vision_profile = profile_for(vision,current_vision) if vision else current_vision
    changes = []
    # JShi treats profiles equal to the global profile as inherited. Mark this
    # per-purpose selection explicitly before applying a new global chat model.
    if vision_profile.get("choices") and (vision_profile != current_vision or
            (chat_profile != global_profile and vision_profile == global_profile)):
        changes.append(("vision",dict(vision_profile,scope="vision")))
    if chat_profile != global_profile:
        changes.append(("all",chat_profile))
    if key and not changes:
        changes.append((None,None))
    for index,(name,profile) in enumerate(changes):
        data = {"name":name,"profile":profile} if name else {}
        if key and index==0:
            data["services"] = {"openai":{"api_key":key}}
        request = Request(origin+"/model-settings",data=json.dumps(data).encode(),
                          headers={"Origin":origin,"Content-Type":"application/json"})
        try:
            with http.open(request,timeout=15) as response:
                json.load(response)
        except (OSError,ValueError) as exc:
            label = {"vision":"图像识别模型","all":"对话模型",None:"服务密钥"}[name]
            raise ValueError(("图像识别模型已保存。" if index else "")+label+"保存失败，请重试") from exc



def start_child(key, args, env):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / (key+".log")).open("ab") as log:
        offset = log.tell()
        child = subprocess.Popen(args, cwd=JSHI, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    processes[key] = child
    return child, offset


def wait_ready(key):
    deadline = time.monotonic()+180
    while time.monotonic()<deadline:
        state = status(key)
        if state=="运行中":
            return
        if state!="启动中":
            break
        time.sleep(.4)
    raise ValueError(f"服务未就绪，请查看 {LOG_DIR / (key+'.log')}")


def switch_device(pid):
    child = processes.get("jshi-app")
    if not child or child.pid != pid or child.poll() is not None:
        raise ValueError("当前实例由另一个窗口启动，请在原窗口正常退出匠石后再切换设置。")
    with http.open(f"http://127.0.0.1:{PORTS['jshi-app']}/status",timeout=3) as response:
        data = json.load(response)
    if data.get("voice_active") or data.get("busy") or data.get("cleanup_stage"):
        raise ValueError("请先结束采集，等当前会话保存后再切换设置。")
    child.send_signal(signal.CTRL_BREAK_EVENT)
    # Wake an idle Windows IOCP loop so its pending Python signal is dispatched.
    try:
        with http.open(f"http://127.0.0.1:{PORTS['jshi-app']}/status", timeout=1):
            pass
    except OSError:
        pass
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("匠石仍在保存并退出，请稍后重试。") from exc


def launch_app(app, options):
    if not isinstance(options, dict):
        raise ValueError("启动参数格式错误")
    with LOCK:
        if app=="webui":
            if status("webui")!="运行中":
                if listener(PORTS["webui"]):
                    raise ValueError("Open WebUI 端口已被占用")
                command = shutil.which("open-webui") or Path(sys.executable).parent/"Scripts"/"open-webui.exe"
                if not Path(command).is_file():
                    command = Path(sys.executable).with_name("open-webui.exe")
                if not Path(command).is_file():
                    raise ValueError("请先安装 Open WebUI 并将 open-webui.exe 加入 PATH。")
                start_child("webui",[str(command),"serve","--host","127.0.0.1","--port",str(PORTS["webui"])],os.environ.copy())
                wait_ready("webui")
            return {"url":f"http://127.0.0.1:{PORTS['webui']}/"}
        if app!="jshi":
            raise ValueError("未知应用")
        return launch_jshi(options)


def launch_jshi(options):
    config = launch_options()
    if options.get("subject","stone") not in config["subjects"] or options.get("mode","vision") not in ("voice","vision"):
        raise ValueError("请选择已有主体和有效入口")
    if options.get("memory") and options["memory"] not in config["memory"]:
        raise ValueError("请选择已有的记忆模块")
    speaker = options.get("speaker","")
    if speaker and speaker not in config["banks"]:
        raise ValueError("未知声纹库")
    asr, tts = voice_choices(options, config)
    if "mlx" in (asr,tts):
        raise ValueError("当前项目使用 Mac 的 MLX 语音服务，请选择 Windows 上可用的语音识别和播报。")
    capture = options.get("capture","desktop")
    address = options.get("phoneIp") if capture=="phone" else None
    if capture not in ("desktop","phone") or (capture=="phone" and address not in config["phoneAddresses"]):
        raise ValueError("请选择此电脑的局域网地址，让 iPhone 与电脑连接同一网络。")
    chat, vision = options.get("chat",""), options.get("vision","")
    for choice, catalog in [(chat,config["models"]), (vision,config["visionModels"])]:
        if choice and choice not in {s+"|"+m for s,models in catalog.items() for m in models}:
            raise ValueError("所选模型不在项目目录中")
    key = options.get("apiKey","").strip()
    if key and not any(c.startswith("openai|") for c in (chat,vision)):
        raise ValueError("请先选择 GPT 模型")
    if (not vision and config["visionLocal"] and config["visionCurrent"]==CHAT and
            (address or options.get("mode","vision")=="vision" or config["visionEnabled"])):
        raise ValueError("项目默认画面识别指向 Mac MLX，请选择此电脑已配置的图像识别模型。")
    args = jshi_args(options,config,asr,tts)
    want_vision = bool(vision) or bool(address) or options.get("mode","vision")=="vision" or config["visionEnabled"]
    settings = {"args":args, "speaker":speaker, "vision":want_vision}
    pid = listener(PORTS["jshi-app"])
    previous = launch_state(pid)
    reused = bool(pid)
    if pid and status("jshi-app")!="运行中":
        raise ValueError("匠石端口已被占用")
    old = previous.get("settings", {})
    # A phone-enabled instance also serves the desktop microphone and camera.
    same = old == settings
    if not same and not address and previous.get("ip") and "--phone" in old.get("args", []):
        same = (old["args"][:old["args"].index("--phone")] == args and old.get("speaker")==speaker
                and (not want_vision or old.get("vision")))
    if pid and not same:
        switch_device(pid)
        reused = False
    if not reused:
        if address and listener(PHONE_PORT):
            raise ValueError(f"手机端口 {PHONE_PORT} 已被占用")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        if speaker:
            env["JSHI_VOICE_SPEAKER_MODEL"] = speaker
        if want_vision:
            env["JSHI_VISION_ENABLED"] = "1"
        child, offset = start_child("jshi-app",args,env)
        state = {"pid":child.pid,"ip":address,"offset":offset,"directory":config["directory"],"settings":settings}
        tmp = PHONE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state),encoding="utf-8")
        tmp.replace(PHONE_STATE)
        wait_ready("jshi-app")
    if chat or vision:
        apply_models(chat,vision,key)
    query = urlencode({"asr":asr,"tts":tts,"speaker":speaker,"source":"phone" if address else ""})
    result = {"url":f"http://127.0.0.1:{PORTS['jshi-app']}/?{query}", "reused":reused}
    if address:
        state = phone_state(listener(PORTS["jshi-app"]))
        if not state:
            raise ValueError("手机 HTTPS 入口尚未就绪，请查看启动日志。")
        result["phone"] = phone_details(state)
    return result
