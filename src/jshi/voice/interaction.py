"""JEV feedback grounded in completed playback, not generated intentions."""
from collections import deque
from threading import RLock
import re
from time import time


class InteractionFeedback:
    def __init__(self, *, clock=time):
        self.clock = clock
        self.lock = RLock()
        self.questions = deque(maxlen=8)
        self.notes = deque(maxlen=2)

    def played(self, reply_id, segment, text, targets, input_ids=()):
        if not re.search(r'[？?]|怎么称呼|如何称呼|请告诉我|能否|是否', text):
            return
        with self.lock:
            key = f'{reply_id}:{segment}'
            if any(row['id'] == key for row in self.questions):
                return
            self.questions.append({'id': key, 'reply_id': reply_id, 'segment': segment,
                'text': text[:240], 'target_ids': list(targets), 'input_ids': list(input_ids),
                'at_ms': self.clock()*1000, 'state': 'awaiting_answer', 'basis': '播放器确认完整播出'})

    def incoming(self, input_id, actor_id, text, *, directed, received_at_ms=None):
        if not directed:
            return
        at = received_at_ms if received_at_ms is not None else self.clock()*1000
        with self.lock:
            for row in self.questions:
                if (row['state'] == 'awaiting_answer' and 0 <= at-row['at_ms'] <= 120000
                    and actor_id in row['target_ids']):
                    row.update(state='possible_answer_received', answer_input_id=input_id,
                               answer_text=text[:160], answer_at_ms=at)

    def main(self, input_ids, note):
        with self.lock:
            for row in self.questions:
                if row.get('answer_input_id') in input_ids:
                    row['state'] = 'answer_candidate_processed'
                    row['processed_at_ms'] = self.clock()*1000
            if note:
                self.notes.append({'text': note[:120], 'input_ids': list(input_ids),
                                   'at_ms': self.clock()*1000, 'basis': '主流程短评，未对外说出'})

    def snapshot(self, *, cutoff_ms=None):
        now = self.clock()*1000
        cutoff = min(now, cutoff_ms) if cutoff_ms is not None else now
        with self.lock:
            questions = []
            for row in self.questions:
                if not 0 <= cutoff-row['at_ms'] <= 120000:
                    continue
                copy = dict(row)
                if copy.get('answer_at_ms', 0) > cutoff:
                    copy = {k:v for k,v in copy.items() if not k.startswith('answer_')}
                    copy['state'] = 'awaiting_answer'
                elif copy.get('processed_at_ms', 0) > cutoff:
                    copy.pop('processed_at_ms', None)
                    copy['state'] = 'possible_answer_received'
                questions.append(copy)
            return {'questions': questions, 'unfinished_notes': [dict(row) for row in self.notes
                    if 0 <= cutoff-row['at_ms'] <= 120000],
                    'rule': '回答候选不等于已解决；对话接续不证明姓名；短评不是人物原话或新的声音证据'}
