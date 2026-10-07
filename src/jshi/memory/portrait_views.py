"""Bounded, persisted person views; background condensation never blocks entry."""
from hashlib import sha256
import json
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4

CAPS = (800, 400, 200)


def bounded_portrait(text, cap):
    text = str(text or '').strip()
    if len(text) <= cap:
        return text
    # Legacy fallback is explicitly an excerpt, not a completed condensed dossier.
    suffix = '（历史肖像节选，完整三档正在后台整理。）'
    kept = ''
    for sentence in re.findall(r'.+?(?:[。！？\n]|$)', text):
        if len(kept + sentence + suffix) > cap:
            break
        kept += sentence
    if kept:
        return kept + suffix
    pending = '人物肖像正在后台浓缩；不据未提供的长档补造人物事实。'
    return pending if len(pending) <= cap else '肖像待浓缩' if cap >= 5 else ''


class PortraitViews:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = RLock()
        self.rows = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    @staticmethod
    def source(raw):
        values = [str(v).strip() for v in (raw.get('levels') or {}).values() if isinstance(v, str)]
        return max(values, key=len, default=str(raw.get('visible_summary') or '').strip())

    def get(self, raw):
        key = raw['subject_id'] + '/' + raw['object_id']
        digest = sha256(self.source(raw).encode()).hexdigest()
        with self.lock:
            item_path = self.path.with_suffix('.views') / (sha256(key.encode()).hexdigest() + '.json')
            row = json.loads(item_path.read_text(encoding='utf-8')) if item_path.exists() else self.rows.get(key)
            if row and row['hash'] == digest:
                return {**raw, 'levels': row['levels'], 'visible_summary': row['levels']['L1']}, True
        text = self.source(raw)
        return {**raw, 'levels': {f'L{i}':bounded_portrait(text, cap) for i,cap in enumerate(CAPS,1)}}, not bool(text)

    def refresh(self, raw, llm):
        if not raw or llm is None or self.get(raw)[1]:
            return
        source = self.source(raw)
        messages = [
            {'role':'system', 'content':'将已有白描人物肖像浓缩为三档，只输出JSON：{"levels":{"L1":"","L2":"","L3":""}}。L1最多800字符，L2最多400，L3最多200，含标点。每档都是完整连贯的人物精华，不罗列聊天清单；L2压缩L1，L3压缩L2。保留身份关系、关键变化和否定/不确定限定，不虚构，不把模型曾说过的话当已验证的人物事实。不把程序阈值、JEV机制或模型能力宣称写成人物信息。可删除无关旧经历，不为填满上限而扩写。'},
            {'role':'user','content':source}]
        for attempt in range(3):
            data = llm.complete_json('person_portrait', messages)
            levels = data.get('levels', {}) if isinstance(data, dict) else {}
            previous, valid = float('inf'), True
            for i,cap in enumerate(CAPS,1):
                value = levels.get(f'L{i}')
                if not isinstance(value,str) or not value.strip() or len(value.strip()) > min(cap,previous):
                    valid = False
                    break
                levels[f'L{i}'] = value.strip()
                previous = len(value.strip())
            if valid:
                break
            lengths = {k:len(v) if isinstance(v,str) else None for k,v in levels.items()}
            with self.lock:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                with self.path.with_suffix('.failures.jsonl').open('a',encoding='utf-8') as file:
                    file.write(json.dumps({'subject_id':raw['subject_id'],'object_id':raw['object_id'],
                        'attempt':attempt+1,'lengths':lengths,'output':data},ensure_ascii=False)+'\n')
            if attempt == 2:
                raise ValueError('人物三档肖像缺失、超长或未逐档浓缩')
            messages.append({'role':'user','content':'上次三档输出不合约，实际字符数：'+json.dumps(lengths,ensure_ascii=False)+
                '。请重新浓缩为L1/L2/L3三段，建议控制在650/300/150字符内，硬上限800/400/200，不为达到字数填聊天细节。只输出levels对象。'})
        row = {'hash':sha256(source.encode()).hexdigest(),'levels':{f'L{i}':levels[f'L{i}'] for i in range(1,4)}}
        with self.lock:
            self.rows[raw['subject_id'] + '/' + raw['object_id']] = row
            key = raw['subject_id'] + '/' + raw['object_id']
            target = self.path.with_suffix('.views') / (sha256(key.encode()).hexdigest() + '.json')
            target.parent.mkdir(parents=True,exist_ok=True)
            temp = target.with_suffix('.' + uuid4().hex + '.tmp')
            temp.write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
            temp.replace(target)
