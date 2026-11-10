"""Shared recent dialogue with player-confirmed delivery, separate from memory."""
from __future__ import annotations

from dataclasses import replace
import json
import re
from pathlib import Path
from threading import RLock
from time import time

from .delivery import sentences


class VoiceDialogue:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = RLock()
        self._hidden = {}
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'include_unplayed': False, 'inputs': [], 'replies': []}

    @property
    def include_unplayed(self):
        with self.lock:
            return self.state['include_unplayed']

    def _save(self):
        self._hidden.clear()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.path)

    def configure(self, value):
        if type(value) is not bool:
            raise ValueError('未播出回复开关须为布尔值')
        with self.lock:
            self.state['include_unplayed'] = value
            self._save()

    def import_history(self, subject, records):
        with self.lock:
            loaded = self.state.setdefault('legacy_loaded', [])
            if subject in loaded:
                return
            latest, first_at, played_clocks = {}, {}, {}
            for record in records:
                if record.event_type == 'voice_delivery' and record.content.get('reply_id'):
                    rid=record.content['reply_id']
                    if rid not in latest or record.created_at > latest[rid].created_at:
                        latest[rid] = record
                    at=record.created_at.timestamp()*1000
                    first_at[rid]=min(first_at.get(rid,at),at)
                elif record.event_type == 'voice_playback_clock' and record.content.get('phase') == 'completed':
                    value=record.content
                    at=value.get('received_at_ms')
                    if type(at) in (int,float) and 0 < at < 1e15:
                        played_clocks.setdefault(value.get('reply_id'),{})[str(value.get('segment'))]=at
            existing = {r['reply_id'] for r in self.state['replies']}
            for reply_id, record in latest.items():
                if reply_id in existing:
                    continue
                value = record.content
                played = value.get('played_text') or ''
                texts = sentences(played + (value.get('partial_text') or '') + (value.get('pending_text') or ''))
                # A later stop/connection-close snapshot is not a new playback.
                # Prefer actual receipts; otherwise retain the earliest evidence.
                at = first_at[reply_id]
                length, completed = 0, []
                for i, text in enumerate(texts):
                    length += len(text)
                    if length <= len(played):
                        completed.append(i)
                self.state['replies'].append({'subject':subject,'key':'legacy:'+reply_id,
                    'session':value.get('session_id','legacy'),'reply_id':reply_id,
                    'texts':list(texts),'played':completed,'state':value.get('state','generated'),
                    'at':at,'played_at':{str(i):played_clocks.get(reply_id,{}).get(str(i),at) for i in completed}})
            self.state['replies'] = self.state['replies'][-128:]
            loaded.append(subject)
            self._save()

    def input(self, subject, event_id, text, label, at=None, *, actor_id=""):
        with self.lock:
            rows = self.state['inputs']
            if not any(r['subject'] == subject and r['id'] == event_id for r in rows):
                rows.append({'subject': subject, 'id': event_id, 'text': text, 'who': label,
                             'at': at if at is not None else time()*1000, 'actor_id':actor_id})
                self.state['inputs'] = rows[-256:]
                self._save()

    def plan(self, subject, key, session, texts, *, shown=False, hide_until_played=True):
        """Register a reply. Waiting-ack text is already committed by the host, so keep it
        visible to the model without waiting for player receipts."""
        with self.lock:
            if not any(r['subject'] == subject and r['key'] == key for r in self.state['replies']):
                self.state['replies'].append({'subject': subject, 'key': key, 'session': session,
                    'reply_id': '', 'texts': list(texts), 'played': list(range(len(texts))) if shown else [],
                    'state': 'shown' if shown else 'generated', 'at': time()*1000,
                    'hide_until_played': bool(hide_until_played)})
                self.state['replies'] = self.state['replies'][-128:]
                self._save()

    def link_inputs(self, subject, ids, target, label, *, link_id, active=True):
        """A reversible projection; preserve the original label and actor."""
        with self.lock:
            for row in self.state['inputs']:
                if row['subject'] != subject or row['id'] not in ids:
                    continue
                projection = row.setdefault('person_links', {})
                if active:
                    projection[link_id] = {'actor_id':target,'who':label}
                else:
                    projection.pop(link_id,None)
            self._save()

    def revise_input(self, subject, event_ids, text):
        with self.lock:
            rows = [r for r in self.state['inputs'] if r['subject'] == subject and r['id'] in event_ids]
            if rows:
                rows.sort(key=lambda r: r['at'])
                rows[0]['text'] = text
                removed = {r['id'] for r in rows[1:]}
                self.state['inputs'] = [r for r in self.state['inputs']
                    if not (r['subject'] == subject and r['id'] in removed)]
                self._save()

    def bind(self, key, reply_id):
        with self.lock:
            row = next((r for r in self.state['replies'] if r['key'] == key), None)
            if row:
                row['reply_id'] = reply_id
                self._save()

    def played(self, reply_id, segment):
        with self.lock:
            row = next((r for r in self.state['replies'] if r['reply_id'] == reply_id), None)
            if row and 0 <= segment < len(row['texts']) and segment not in row['played']:
                row['played'].append(segment)
                row.setdefault('played_at', {})[str(segment)] = time()*1000
                self._save()

    def stop(self, session):
        with self.lock:
            for row in self.state['replies']:
                if row['session'] == session and len(row['played']) < len(row['texts']):
                    row['state'] = 'stopped'
            self._save()

    def recent(self, subject, *, cutoff=None, exclude=(), session=None):
        cutoff = time()*1000 if cutoff is None else cutoff
        with self.lock:
            rows = [{'who': list(r.get('person_links', {}).values())[-1]['who'] if r.get('person_links') else r['who'], 'text': r['text'], 'at': r['at'], 'basis': '收到的原话'}
                    for r in self.state['inputs'] if r['subject'] == subject and r['id'] not in exclude
                    and 0 <= cutoff-r['at'] <= 300000]
            for r in self.state['replies']:
                if r['subject'] != subject or not 0 <= cutoff-r['at'] <= 300000:
                    continue
                for i, text in enumerate(r['texts']):
                    at = r.get('played_at', {}).get(str(i), r['at'])
                    played = i in r['played'] and at <= cutoff
                    if played or self.state['include_unplayed']:
                        rows.append({'who': '匠石', 'text': text, 'at': at if played else r['at'],
                            'basis': ('文字回复已发送，未确认阅读' if r['state'] == 'shown' else '已播') if played
                                     else '已生成但未确认播出' + ('，已中断' if r['state'] == 'stopped' else ''),
                            **({'scope':'previous_session'} if session and r.get('session') != session else {})})
            rows.sort(key=lambda r: r['at'])
            selected, size = [], 0
            for row in reversed(rows[-24:]):
                cost = len(json.dumps(row, ensure_ascii=False))
                if size+cost > 4000:
                    break
                selected.append(row); size += cost
            return list(reversed(selected))

    def note(self, subject, **kwargs):
        rows = self.recent(subject, **kwargs)
        if not rows:
            return ''
        # Human labels and relative times only; internal record IDs stay in storage.
        now = time()*1000
        return '【共享近期对话·原话与实际交付】\n已播=确认整句播出；部分播放=曾开始，具体进度未知；未播=尚未播放。\n' + '\n'.join(
            f"{round(max(0, now-r['at'])/1000)}秒前 {r['who']}（{r['basis']}）：{r['text']}" for r in rows)

    def filter_text(self, text, *, subject=None):
        """Remove known unplayed text from derived context, never infer user intent."""
        if not text or self.include_unplayed:
            return text
        with self.lock:
            if subject not in self._hidden:
                self._hidden[subject] = tuple(sorted({segment for row in self.state['replies']
                    for i, segment in enumerate(row['texts']) if (subject is None or row['subject'] == subject)
                    and i not in row['played'] and row.get('hide_until_played', True)
                    and len(segment.strip()) >= 4}, key=len, reverse=True))
            hidden = self._hidden[subject]
        # Older pending-scene records carry explicit speech-plan lines. They are
        # source formatting, not a keyword classifier for people's utterances.
        text = '\n'.join(line for line in text.split('\n')
            if not re.match(r'^\s*(?:B\d+(?:\[[^\]\n]*\])*\s+)?我准备说', line))
        for segment in hidden:
            for form in {segment, json.dumps(segment, ensure_ascii=False)[1:-1],
                         json.dumps(segment, ensure_ascii=True)[1:-1]}:
                text = text.replace(form, '〔未播出正文已隐藏〕')
        return text

    def clean(self, value, *, subject=None):
        if isinstance(value, str):
            return self.filter_text(value, subject=subject)
        if isinstance(value, dict):
            if not self.include_unplayed and value.get('kind') == 'speech_plan':
                return {'kind': 'speech_plan', 'text': '〔未播出正文已隐藏〕'}
            return {k: self.clean(v, subject=subject) for k, v in value.items()
                    if self.include_unplayed or k not in {'pending_text', 'partial_text', 'pending'}}
        if isinstance(value, (tuple, list)):
            return type(value)(self.clean(v, subject=subject) for v in value)
        return value

    def request(self, request):
        # Preserve the actual current input even when it quotes an unplayed answer.
        subject = request.subject_state.subject_id
        texts = {request.input_text, *[r.get('text','') for r in request.input_items]}
        protected = {}
        user = request.persona_user_text
        for i, text in enumerate(sorted(filter(None, texts), key=len, reverse=True)):
            marker = f'\x00CURRENT_INPUT_{i}\x00'
            user = user.replace(text, marker)
            protected[marker] = text
        user = self.filter_text(user, subject=subject)
        for marker, text in protected.items():
            user = user.replace(marker, text)
        return replace(request, context=self.clean(request.context, subject=subject),
                       transport_context=self.filter_text(request.transport_context, subject=subject),
                       persona_user_text=user)


def dialogue_state(process):
    state = getattr(process, 'voice_dialogue', None)
    if state is None:
        state = VoiceDialogue(process.repository.path.parent/'voice_dialogue.json')
        process.voice_dialogue = state
    return state


def delivery_brief(snapshot, target=""):
    """Project playback facts without transport IDs or repeated target lists."""
    lines = [('对' + target + '的' if target else '') + '播放中断' + ('（连接断开）' if snapshot.get('reason') == 'connection closed' else '') + '。']
    for key, label in (('played_text', '已播'), ('partial_text', '部分播放（具体进度未知）'), ('pending_text', '未播')):
        if snapshot.get(key):
            lines.append(label + '｜匠石：' + snapshot[key])
    return '\n'.join(lines)
