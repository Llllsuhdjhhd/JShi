"""Explicit, budget-bounded online diagnosis. No writes to live memory or voice banks."""
import argparse
import base64
from dataclasses import asdict, replace
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from time import perf_counter
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from jshi.core.skillconfig import SkillConfigStore, build_model_port
from jshi.voice.jev import VoiceJEV
from jshi.vision.config import VisionConfig


def main(output):
    # Load the project's existing credentials without printing or persisting them.
    for line in Path('.env').read_text(encoding='utf-8-sig').splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=',1)
            os.environ.setdefault(key.strip(), value.strip().strip('\"\''))
    store = SkillConfigStore()
    from jshi.app.cli import _skills_config_path
    path = _skills_config_path()
    if path:
        store.load_file(path)
    base = build_model_port(store.profile('voice_jev'))
    config = VisionConfig.from_env()
    endpoint = getattr(base, 'endpoint', '')
    key = getattr(base, 'api_key', '') or config.api_key
    if urlparse(endpoint).hostname != 'api.deepseek.com' or not key:
        raise ValueError('测试仅允许已配置的官方 DeepSeek 接口')
    report = {'budget_cny':1,'estimated_cost_cny':0,'calls':[]}
    reserved = 0
    def send(payload, label):
        nonlocal reserved
        # Reserve a conservative 65k input-token allowance per call, including image.
        # Flash peak rates: input 2 CNY/M, output 8 CNY/M; max 1200 output tokens.
        ceiling = .130 + payload['max_tokens'] * 8 / 1_000_000
        if reserved + ceiling > 1:
            raise ValueError('已达到测试预算，停止请求')
        reserved += ceiling
        payload['model']='deepseek-flash'
        payload['thinking']={'type':'disabled'}
        payload['stream']=False
        if len(json.dumps(payload.get('messages'),ensure_ascii=False).encode())>250_000:
            raise ValueError('测试输入超出预算保护范围')
        started=perf_counter()
        row={'test':label}
        report['calls'].append(row)
        try:
            req=Request(endpoint,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            with urlopen(req,timeout=30) as r: result=json.load(r)
            usage=result.get('usage',{})
            cost=(usage.get('prompt_tokens',0)*2+usage.get('completion_tokens',0)*8)/1_000_000
            report['estimated_cost_cny']+=cost
            row.update(usage=usage,estimated_cost_cny=cost,finish_reason=result['choices'][0].get('finish_reason'),response=result['choices'][0]['message'].get('content'),status='succeeded')
            return result
        except Exception as e:
            row.update(status='failed',error_type=type(e).__name__,http_status=getattr(e,'code',None))
            raise
        finally:
            row['ms']=round((perf_counter()-started)*1000)
            Path(output).parent.mkdir(parents=True,exist_ok=True)
            Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    from jshi.models.base import _chat_payload, ModelResponse
    class Model:
        name='deepseek-flash'
        def generate(self,request):
            result=send(json.loads(_chat_payload(self.name,request,thinking='disabled',max_tokens=800)),'jev_'+str(request.input_text.find('overlap')>=0))
            return ModelResponse(text=result['choices'][0]['message']['content'],model=self.name)
    for overlap in (False,True):
        decision=VoiceJEV(Model()).decide_batch('stone',[{'n':1,'text':'匠石，你听得到我吗？为什么没有回应？','who':'unknown','at':1,'overlap':overlap}],[],{'state':'idle'},overlap=overlap)
        report['calls'][-1]['parsed']={'action':decision.action,'kept':all(i.keep for i in decision.items),'judged':decision.judged,'model_error':decision.model_error}
    import ultralytics
    public_sample = Path(ultralytics.__file__).parent/'assets'/'bus.jpg'
    if public_sample.is_file():
        from PIL import Image
        image=Image.open(public_sample).convert('RGB');image.thumbnail((640,640));out=BytesIO();image.save(out,'JPEG',quality=65)
        send({'max_tokens':800,'messages':[{'role':'user','content':[{'type':'text','text':'用一段简短中文描述可见现场。不要推断人物姓名。'}, {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()}}]}]},'public_visual_sample')
    report['reserved_upper_cost_cny']=reserved
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'calls':len(report['calls']),'estimated_cost_cny':report['estimated_cost_cny'],'results':[{k:r.get(k) for k in ['test','ms','status','parsed']} for r in report['calls']]}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    main(parser.parse_args().output)
