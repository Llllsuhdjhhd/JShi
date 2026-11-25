"""Exercise the Windows workbench against an isolated JShi instance."""
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, CookieJar
from aiohttp.test_utils import unused_port
from tools.model_desk import services


def test_windows_interface_names_and_localized_listeners(monkeypatch):
    addresses = [{"IPAddress":"127.0.0.1","InterfaceAlias":"Loopback"},
                 {"IPAddress":"192.168.1.23","InterfaceAlias":"无线网络"}]
    def run(command, **kwargs):
        if command[0]=="powershell.exe":
            return SimpleNamespace(returncode=0, stdout=json.dumps(addresses,ensure_ascii=False).encode("utf-8-sig"))
        assert command==["netstat.exe","-ano","-p","TCP"]
        return SimpleNamespace(returncode=0, stdout=b" TCP  127.0.0.1:8767  192.168.1.10:8767  STATE  99\n"
                               b" TCP  192.168.1.23:8767  0.0.0.0:0  STATE  42\n")
    monkeypatch.setattr(subprocess,"run",run)
    assert services.lan_addresses()=={"192.168.1.23":"无线网络"}
    assert services.listener(8767)==42
    assert services.listener(8766) is None


def test_launch_phone_pair_reuse_and_exit(tmp_path,monkeypatch):
    data=tmp_path/"独立存档"
    data.mkdir()
    (data/"identities.json").write_text(json.dumps([{"subject_id":"stone","name":"匠石","origin":"test"}]),encoding="utf-8")
    for k,v in {
        "JSHI_DATA_DIR":str(data), "PYTHONPATH":str(services.JSHI/"src"),
        "JSHI_MEMORY_BACKEND":"inprocess","JSHI_VOICE_API_KEY":"test-fixture",
        "JSHI_TOOL_ENGINE":"stub","JSHI_VISION_CLOUD_ENABLED":"false",
        "JSHI_VISION_MODEL":"test-camera","JSHI_VISION_ENDPOINT":"http://127.0.0.1:1/v1",
        "JSHI_ENV_FILE":str(tmp_path/"no-env")
    }.items():
        monkeypatch.setenv(k,v)
    logs=tmp_path/"logs"
    monkeypatch.setattr(services,"LOG_DIR",logs)
    monkeypatch.setattr(services,"PHONE_STATE",logs/"launch.json")
    port=unused_port()
    phone_port=unused_port()
    while phone_port==port:
        phone_port=unused_port()
    monkeypatch.setitem(services.PORTS,"jshi-app",port)
    monkeypatch.setattr(services,"PHONE_PORT",phone_port)
    monkeypatch.setattr(services,"processes",{})
    monkeypatch.setattr(services,"lan_addresses",lambda:{"127.0.0.1":"test adapter"})
    if os.name!="nt":
        # Keep real network, JShi subprocess, TLS and pairing. Adapt the three OS calls.
        monkeypatch.setattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0,raising=False)
        monkeypatch.setattr(signal,"CTRL_BREAK_EVENT",signal.SIGINT,raising=False)
        def listener(number):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1",number))!=0:
                    return None
            return services.processes["jshi-app"].pid
        monkeypatch.setattr(services,"listener",listener)

    async def pair(phone):
        cert=data/"phone-tls"/"127.0.0.1"/"iphone-ca.cer"
        trust=ssl.create_default_context(cadata=cert.read_bytes())
        root=phone["url"].rstrip("/")
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as session:
            async with session.post(root+"/pair",headers={"Origin":root},data=phone["code"],ssl=trust) as response:
                assert response.status==200
            async with session.ws_connect(root+"/device-link?playback=1",headers={"Origin":root},ssl=trust) as device:
                assert (await device.receive_json(timeout=3))["type"]=="available"

    options={"capture":"phone","phoneIp":"127.0.0.1","mode":"voice","asr":"online","tts":"online"}
    try:
        first=services.launch_app("jshi",options)
        assert not first["reused"] and "source=phone" in first["url"]
        asyncio.run(pair(first["phone"]))
        assert services.launch_app("jshi",options)==dict(first,reused=True)
        child=services.processes["jshi-app"]
        services.switch_device(child.pid)
        assert child.poll() is not None
        assert services.listener(phone_port) is None
        assert services.listener(port) is None
    finally:
        child=services.processes.get("jshi-app")
        if child and child.poll() is None:
            child.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
