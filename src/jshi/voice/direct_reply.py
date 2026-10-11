"""Session-only materials for JEV's short-chat exit; no memory writes."""
from collections import deque
from threading import RLock


class DirectReplyState:
    def __init__(self):
        self.lock = RLock()
        self.people = {}
        self.recent = deque(maxlen=8)

    def prime(self, subject, result):
        current = result.current_state
        if not current.speaker or not current.speaker.object_id:
            return
        reports = {r.source:r for r in current.source_report}
        if ('person_portrait' not in reports or any(reports[name].error or reports[name].status != 'implemented'
               for name in ('person_portrait', 'person_experience') if name in reports)):
            return
        oid = current.speaker.object_id
        material = [f.content[:400] for f in current.fragments
                    if f.source in ('person_portrait', 'person_experience') and f.object_id in (None, oid)]
        with self.lock:
            self.people.pop((subject, oid), None)
            self.people[(subject, oid)] = {'name': current.speaker.label,
                'materials':material[:2], 'identity':current.subject_state.identity_summary[:300],
                'stance':current.subject_state.current_stance[:200]}
            if len(self.people) > 64:
                self.people.pop(next(iter(self.people)))
            self.recent.append({'subject':subject, 'object_id':oid, 'input':current.input_text[:200],
                                'reply':result.response_plan.verbal_text()[:200]})

    def context(self, subject, object_ids):
        object_ids = tuple(object_ids)
        ids = tuple(dict.fromkeys(object_ids))
        with self.lock:
            ready = len(ids) == 1 and bool(ids[0]) and (subject, ids[0]) in self.people
            return {'allowed':ready, 'reason':'人物材料已由主流程加载' if ready else '人物材料尚未加载或当前多人/身份未明',
                'person': self.people.get((subject, ids[0])) if ready else None,
                'recent':[r for r in self.recent if ready and r['subject']==subject and r['object_id']==ids[0]][-4:]}

    def accept(self, subject, oid, text, reply):
        if not self.context(subject, [oid])['allowed']:
            return False
        with self.lock:
            self.recent.append({'subject':subject,'object_id':oid,'input':text[:200],'reply':reply[:200]})
        return True


def reply_state(process):
    state = getattr(process, '_jev_direct_reply_state', None)
    if state is None:
        state = DirectReplyState()
        process._jev_direct_reply_state = state
    return state
