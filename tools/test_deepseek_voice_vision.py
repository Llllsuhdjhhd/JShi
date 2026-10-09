"""Explicit, budget-bounded online diagnosis. No writes to live memory or voice banks."""
import argparse
import base64
from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from jshi.core.skillconfig import SkillConfigStore, build_model_port
from jshi.voice.jev import VoiceJEV
from jshi.vision.config import VisionConfig


def main(output, budget=1.0, crowded=False):
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
    if not 0 < budget <= 1:
        raise ValueError('测试预算必须在0到1元之间')
    report = {'budget_cny':budget,'estimated_cost_cny':0,'calls':[]}
    reserved = 0
    def send(payload, label):
        nonlocal reserved
        messages = payload.get('messages', [])
        has_image = any(isinstance(m.get('content'), list) and any(
            p.get('type') == 'image_url' for p in m['content']) for m in messages)
        # UTF-8 byte count safely overestimates plain-text tokenization. Public
        # images are limited to 640px below; reserve 65k input tokens for them.
        message_bytes = len(json.dumps(messages, ensure_ascii=False).encode())
        input_ceiling = 65_000 if has_image else message_bytes + 1024
        ceiling = (input_ceiling * 2 + payload['max_tokens'] * 8) / 1_000_000
        if reserved + ceiling > budget:
            raise ValueError('已达到测试预算，停止请求')
        reserved += ceiling
        report['reserved_upper_cost_cny'] = reserved
        payload['model']='deepseek-flash'
        payload['thinking']={'type':'disabled'}
        payload['stream']=False
        if message_bytes > 250_000:
            raise ValueError('测试输入超出预算保护范围')
        started=perf_counter()
        row={'test':label, 'reserved_upper_cost_cny':ceiling}
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
            result=send(json.loads(_chat_payload(self.name,request,thinking='disabled',max_tokens=1600 if crowded else 800)),
                        'crowded_shared_jev' if crowded else 'jev_'+str(request.input_text.find('overlap')>=0))
            return ModelResponse(text=result['choices'][0]['message']['content'],model=self.name)
    noise = ['我已经', 'J', 'T THE WEST', "WE\u0027LL SEE YOU WHAT YOU", '我一定是跟',
             '陆广，作业做好了吗？', '那两本书放在桌上', '嗯', '十', '下周考试复习到哪了？']
    scenarios = [(True, [{'n':i,'text':text,'who':'unknown','at':i*1000,
                         'overlap':i in {3,5}, 'source':'voice'} for i,text in enumerate(
        noise + ['匠石，你听得到我吗？', '匠石，看看画面里的人站在哪里。'],1)])] if crowded else [
        (overlap,[{'n':1,'text':'匠石，你听得到我吗？为什么没有回应？','who':'unknown','at':1,'overlap':overlap}])
        for overlap in (False,True)]
    for overlap,batch in scenarios:
        decision=VoiceJEV(Model()).decide_batch('stone',batch,[],{'state':'thinking',
            'jev_scene':'室内测试现场，有人在写作业、旁人互聊；只有明确向匠石的发言需要考虑回复。'},overlap=overlap)
        report['calls'][-1]['parsed']={'action':decision.action,'kept':all(i.keep for i in decision.items),'judged':decision.judged,'model_error':decision.model_error}
        report['calls'][-1]['parsed'].update(input_count=len(batch), output_count=len(decision.items),
            main_prompt_hint=decision.main_prompt_hint,
            overlap_identity_unknown=all(i.speaker_pick=='unknown' for i in decision.items if batch[i.n-1].get('overlap')))
    import ultralytics
    public_sample = Path(ultralytics.__file__).parent/'assets'/'bus.jpg'
    if public_sample.is_file():
        from PIL import Image
        image=Image.open(public_sample).convert('RGB');image.thumbnail((640,640));out=BytesIO();image.save(out,'JPEG',quality=65)
        from jshi.vision.model import VisionModel
        visual = VisionModel(replace(config, model='deepseek-flash'),
            lambda payload: send(payload,'public_visual_with_background_speech'))
        description = visual.describe(out.getvalue(), previous='先前看到一辆公交车和数名行人。',
            scene='有人说“我已经”“J”“T THE WEST”，旁人问作业和考试。有人说“我是lux”。这些是测试背景声音，不能当作画面人物身份。')
        report['visual_description_valid'] = bool(description)
    report['reserved_upper_cost_cny']=reserved
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'calls':len(report['calls']),'estimated_cost_cny':report['estimated_cost_cny'],'results':[{k:r.get(k) for k in ['test','ms','status','parsed']} for r in report['calls']]}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--budget-cny',type=float,default=1.0)
    parser.add_argument('--crowded',action='store_true',help='模拟截图中的无关短句、旁人互聊、重叠与直接提问')
    args=parser.parse_args()
    main(args.output,args.budget_cny,args.crowded)
