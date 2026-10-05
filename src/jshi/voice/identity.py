from __future__ import annotations
from dataclasses import replace
import re
from time import monotonic


from jshi.core.envelope import SpeakerEvidence
from jshi.recognition import CarrierEntry, ObjectProfile, new_object_id
from jshi.recognition.port import SpeakerCandidate
from jshi.subject.domain import HistoryKind, HistoryRecord


INTERNAL_PREFIX = '未命名访客'


def internal_name(profiles) -> str:
    """Numbers are never reused: a named visitor keeps the old internal name as an alias."""
    used = [int(m.group(1)) for p in profiles.list() for name in (p.label, *p.aliases)
            if (m := re.fullmatch(INTERNAL_PREFIX + r' (\d+)', name))]
    return f'{INTERNAL_PREFIX} {max(used, default=0) + 1}'


def name_correction(text: str, current: str, *, anonymous: bool) -> str:
    """A typed claim of one's own name. Returns the new name, or '' if none is clear."""
    text = text.strip()
    patterns = [r'我(?:其实|实际上|真的)?(?:是|叫)\s*(?P<new>[^，,。！!？?\s]{1,20})\s*[，,]?\s*(?:不是|不叫)\s*(?P<old>[^，,。！!？?\s]{1,20})',
                r'我(?:其实)?(?:不是|不叫)\s*(?P<old>[^，,。！!？?\s]{1,20})\s*[，,]?\s*(?:我)?(?:是|叫)\s*(?P<new>[^，,。！!？?\s]{1,20})',
                r'(?:我的)?名字(?:应该|其实)?(?:是|叫)\s*(?P<new>[^，,。！!？?\s]{1,20})']
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        old = match.groupdict().get('old')
        if old is not None and old != current:
            return ''
        new = match.group('new')
        break
    else:
        if not anonymous:
            return ''
        from .jev import explicit_name
        new = explicit_name(text)
    new = new.strip().rstrip('。！!，,')
    valid = re.fullmatch(r'[\u4e00-\u9fff]{2,6}|[A-Za-z][A-Za-z ]{0,19}', new or '')
    return new if valid and new != current else ''


def canonical_voice_profile(profiles, profile):
    if profile.source == 'voice_self_introduction':
        from .jev import explicit_name
        name = explicit_name('我是 ' + profile.label)
        if name and name != profile.label:
            matches = [p for p in profiles.find_by_names(name)
                       if p.object_id != profile.object_id and p.status != 'rejected']
            if len(matches) == 1:
                return matches[0]
    return profile


class VoiceIdentities:
    """Diarization ids are session-local; device ids never identify a person."""
    def __init__(self, profiles, subject_id: str, session_id: str, repository=None) -> None:
        self.profiles = profiles
        self.subject_id = subject_id
        self.session_id = session_id
        self.repository = repository
        self.tracks: dict[str, SpeakerEvidence] = {}
        self.clusters: dict[str, SpeakerEvidence] = {}
        self.associations: dict[str, SpeakerEvidence] = {}
        self.name_questions = {}
        self.unassigned = SpeakerEvidence('unassigned', new_object_id(), '声音归属待定', 'unknown', 'unassigned_audio')

    def name_question_played(self, object_id, text):
        if re.search(r'[？?]', text) and re.search(r'怎么称呼|如何称呼|叫什么(?:名字)?|你的名字|您叫什么', text):
            profile = self.profiles.get(object_id)
            if profile and profile.source == 'voice_anonymous' and profile.label.startswith(INTERNAL_PREFIX):
                self.name_questions[object_id] = monotonic()

    def name_answer(self, evidence, text):
        """A bare name is meaningful only as this person's answer to a played question."""
        target = evidence
        if evidence.method == 'unassigned_audio':
            # A short name often cannot yield an embedding. Only an immediate
            # answer on the addressed track can use the pending dialogue link.
            previous = self.tracks.get(evidence.track_id)
            open_questions = [o for o, at in self.name_questions.items() if monotonic()-at <= 8]
            if not previous or open_questions != [previous.object_id]:
                return ''
            target = previous
        asked = self.name_questions.get(target.object_id)
        if asked is None or monotonic()-asked > 90:
            return ''
        answer = text.strip().strip('。！!，, ')
        answer = re.sub(r'^(?:叫我|名字是|叫)\s*', '', answer)
        if re.match(r'^(?:我|你|他|她|我们|不|没|先|等|随便|好的|谢谢|今天|明天|现在)', answer):
            return ''
        if answer in {'不知道','不告诉你','不想说','随便','好的','你好','没有','不用','是的','不是','为什么','怎么了','听到了','听见了','不需要','谢谢'}:
            return ''
        return answer if re.fullmatch(r'[\u4e00-\u9fff]{2,6}|[A-Za-z][A-Za-z ]{0,19}', answer) else ''

    def associated(self, evidence):
        linked = self.associations.get(evidence.object_id)
        if linked:
            evidence = replace(linked, track_id=evidence.track_id,
                status='unknown', method='voice_continuity', confidence=None, voiceprint_id='') if evidence.method == 'voice_continuity' else replace(linked, track_id=evidence.track_id)
        if evidence.method == 'self_report' or (linked and linked.method == 'self_report'):
            return evidence
        profile = self.profiles.get(evidence.object_id)
        return replace(evidence, label=profile.label) if profile else evidence

    def canonical(self, profile):
        # Earlier self-introduction parsing could register "L U X 你好".
        # Resolve this specific greeting/name artifact without deleting memories
        # or moving voiceprint registrations between stored profiles.
        return canonical_voice_profile(self.profiles, profile)

    def resolve(self, track_id: str, *, voiceprint_id: str = "", confidence: float | None = None, cluster_id: str = "", uncertain: bool = False, tentative: bool = False) -> SpeakerEvidence:
        if voiceprint_id:
            reference = voiceprint_id if voiceprint_id.startswith("local:") else f"volc:{voiceprint_id}"
            profile = self.profiles.find_by_carrier("voiceprint", reference)
            if profile is None and not voiceprint_id.startswith("local:"):
                # Cloud matching may return the registered SpeakerName rather
                # than SpeakId. Our enrollment names are object ids, not labels.
                named = self.profiles.get(voiceprint_id)
                if named is not None and any(c.kind == "voiceprint" and c.value.startswith("volc:") for c in named.carriers):
                    profile = named
            if profile is not None and profile.status != "rejected":
                profile = self.canonical(profile)
                evidence = SpeakerEvidence(track_id, profile.object_id, profile.label,
                    "recognized", "voiceprint_match", confidence, voiceprint_id)
                self.tracks[track_id] = evidence
                if cluster_id:
                    old = self.clusters.get(cluster_id)
                    self.clusters[cluster_id] = evidence
                return evidence
        if uncertain or track_id.startswith(('unidentified-', 'overlap-')):
            return replace(self.unassigned, track_id=track_id)
        if cluster_id:
            if cluster_id.startswith('pending-') and tentative:
                if cluster_id not in self.clusters:
                    count = sum(e.label.startswith('待定声音 ') for e in self.clusters.values()) + 1
                    self.clusters[cluster_id] = SpeakerEvidence(track_id, new_object_id(), f'待定声音 {count}', 'unknown', 'voice_continuity')
                evidence = replace(self.associated(self.clusters[cluster_id]), track_id=track_id)
                self.tracks[track_id] = evidence
                return evidence
            pending = self.clusters.get(cluster_id)
            if pending and pending.label.startswith('待定声音 ') and self.profiles.get(pending.object_id) is None:
                profile = self.profiles.create(ObjectProfile(pending.object_id, internal_name(self.profiles), status='provisional', source='voice_anonymous'))
                self.clusters[cluster_id] = SpeakerEvidence(track_id, profile.object_id, profile.label, 'unknown', 'anonymous_voice_cluster')
            if cluster_id not in self.clusters:
                label = internal_name(self.profiles)
                profile = self.profiles.create(ObjectProfile(new_object_id(), label, status='provisional', source='voice_anonymous'))
                self.clusters[cluster_id] = SpeakerEvidence(track_id, profile.object_id, label, 'unknown', 'anonymous_voice_cluster')
            evidence = replace(self.associated(self.clusters[cluster_id]), track_id=track_id)
            if tentative:
                evidence = replace(evidence, status='unknown', method='voice_continuity', confidence=None, voiceprint_id='')
            self.tracks[track_id] = evidence
            return evidence
        if track_id not in self.tracks:
            # No stable speaker info: each utterance has a separate track, never
            # reuse the preceding speaker merely because it is the same microphone.
            self.tracks[track_id] = SpeakerEvidence(track_id, new_object_id())
        return self.tracks[track_id]

    def introduce(self, track_id: str, name: str, *, basis: str = 'explicit_self_introduction', cluster_id: str = '', uncertain: bool = False) -> tuple[SpeakerEvidence, str]:
        old = self.resolve(track_id, cluster_id=cluster_id, uncertain=uncertain)
        if basis == 'name_answer' and old.method == 'unassigned_audio':
            previous = self.tracks.get(track_id)
            if previous and monotonic()-self.name_questions.get(previous.object_id, -1e20) <= 8:
                old = previous
        if basis != 'manual_selection':
            matches = [p for p in self.profiles.find_by_names(name) if p.status != 'rejected']
            if len(matches) > 1:
                return old, '这个称呼对应多个已有对象，需要进一步确认；不自动关联。'
            if old.status in {'recognized', 'introduced'} and not (self.profiles.get(old.object_id) and self.profiles.get(old.object_id).source == 'voice_anonymous'):
                known = self.profiles.get(old.object_id)
                if known and name not in (known.label, *known.aliases):
                    return old, "自报姓名与入口归属冲突，保留两种证据，待礼貌核对。"
                return old, "自报姓名与当前声纹归属一致；不更改档案。"
            updated = replace(old, label=matches[0].label if len(matches) == 1 else name, status='unknown', method='self_report')
            if old.method != 'unassigned_audio':
                self.tracks[track_id] = updated
                self.associations[old.object_id] = updated
            if cluster_id:
                self.clusters[cluster_id] = updated
            self.name_questions.pop(old.object_id, None)
            if self.repository is not None:
                self.repository.add_history(HistoryRecord(
                    subject_id=self.subject_id, kind=HistoryKind.FACT,
                    event_type='voice_identity_clue',
                    content={'session_id': self.session_id, 'object_id': old.object_id,
                             'reported_name': name, 'basis': basis, 'scope': 'clue_only'}, source_ids=()))
            return updated, f"对方自报称呼{name}，可以这样称呼；仍保留独立内部编号，不迁移历史、不登记声纹。"
        matches = tuple(p for p in self.profiles.find_by_names(name) if p.status != "rejected")
        # A known voice can have several names, including a name shared by others.
        # Self-introducing another name does not move its voiceprint to that person.
        known = self.profiles.get(old.object_id)
        unnamed = known is not None and known.source == 'voice_anonymous' and known.label.startswith(INTERNAL_PREFIX)
        if known is not None and not unnamed and old.status in {"introduced", "recognized"}:
            if matches and not any(p.object_id == old.object_id for p in matches):
                return old, "所报姓名属于另一个已有对象，与当前归属冲突，保留原证据等待核对，不自动合并或添加别名。"
            profile = self.profiles.add_alias(old.object_id, name)
            return replace(old, label=profile.label), f"{name}是{profile.label}的另一个称呼，保留同一人物；同名不自动合并。"
        if len(matches) > 1:
            return old, "这个称呼对应多个已有对象，需要进一步确认；不要自动关联。"
        anonymous = self.profiles.get(old.object_id)
        if matches:
            profile = matches[0]
        elif anonymous is not None and anonymous.source == 'voice_anonymous':
            profile = self.profiles.rename(anonymous.object_id, name, keep_alias=anonymous.label.startswith(INTERNAL_PREFIX))
        else:
            profile = self.profiles.create(ObjectProfile(
                object_id=new_object_id(), label=name, status="provisional", source="voice_manual_annotation" if basis == 'manual_selection' else "voice_self_introduction"))
        updated = SpeakerEvidence(track_id, profile.object_id, profile.label,
                                  "introduced", "manual_annotation" if basis == "manual_selection" else "self_introduction")
        self.name_questions.pop(old.object_id, None)
        if old.method != 'unassigned_audio':
            self.tracks[track_id] = updated
        if cluster_id:
            self.clusters[cluster_id] = updated
        if old.method != 'unassigned_audio':
            self.associations[old.object_id] = updated
        if self.repository is not None:
            self.repository.add_history(HistoryRecord(
                subject_id=self.subject_id, kind=HistoryKind.FACT,
                event_type="voice_identity_binding",
                content={"session_id": self.session_id, "track_id": track_id,
                         "previous_object_id": old.object_id, "object_id": updated.object_id,
                         "basis": basis, "scope": "utterance_only" if old.method == 'unassigned_audio' else "cluster"}, source_ids=()))
        if old.method == 'unassigned_audio':
            return updated, (f"本句自我介绍为 {updated.label}，日常称呼与记忆按此关联。"
                "本句音频未归属到稳定声纹，不把其他归属待定的发言关联给此人。声纹登记情况另行记录。")
        return updated, (f"本次会话的 {track_id} 自我介绍为 {updated.label}，"
                         f"先前临时对象 {old.object_id} 的本会话发言与此人关联。"
                         "日常称呼与记忆按此关联，不需要考问身份。声纹登记情况另行记录。")

    def rename(self, object_id: str, name: str, previous: str, *, basis: str) -> SpeakerEvidence:
        """Correct a name in place; person id, voiceprints and memories stay linked."""
        others = [p for p in self.profiles.find_by_names(name) if p.object_id != object_id and p.status != 'rejected']
        if others:
            raise ValueError(f'已有另一个叫 {name} 的人物，不自动合并；请到登记页确认是否同一人')
        profile = self.profiles.rename(object_id, name, expected_label=previous, keep_alias=True)
        for table in (self.tracks, self.clusters, self.associations):
            for key, evidence in table.items():
                if evidence.object_id == object_id:
                    table[key] = replace(evidence, label=profile.label)
        self.name_questions.pop(object_id, None)
        if self.repository is not None:
            self.repository.add_history(HistoryRecord(
                subject_id=self.subject_id, kind=HistoryKind.FACT, event_type='voice_identity_rename',
                content={'session_id': self.session_id, 'object_id': object_id,
                         'previous_name': previous, 'name': profile.label, 'basis': basis}, source_ids=()))
        return SpeakerEvidence(f'text-{object_id}', object_id, profile.label, 'introduced', 'self_introduction')

    def candidate(self, evidence: SpeakerEvidence) -> SpeakerCandidate:
        profile = self.profiles.get(evidence.object_id)
        # This score permits an unknown but referenceable actor to converse;
        # it is not a fabricated voice similarity score.
        return SpeakerCandidate(
            self.subject_id, evidence.object_id, evidence.label,
            aliases=profile.aliases if profile else (), confidence=0.7,
            status="provisional" if evidence.status == "unknown" else (profile.status if profile else "provisional"),
            reason="voice_pending" if evidence.method == "voice_continuity" and evidence.label.startswith('待定声音 ') else ("voice_scene" if evidence.method == "voice_scene" else ("voice_unknown" if evidence.status == "unknown" else evidence.method)),
        )


def attach_voiceprint(profiles, object_id: str, voiceprint_id: str) -> None:
    """Attach only after the cloud has successfully registered the sample."""
    profile = profiles.get(object_id)
    if profile is None:
        raise ValueError("unknown object for voiceprint")
    profiles.add_carrier(object_id, CarrierEntry("voiceprint", f"volc:{voiceprint_id}", "volc_registration"))
