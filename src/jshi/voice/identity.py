from __future__ import annotations
from dataclasses import replace


from jshi.core.envelope import SpeakerEvidence
from jshi.recognition import CarrierEntry, ObjectProfile, new_object_id
from jshi.recognition.port import SpeakerCandidate
from jshi.subject.domain import HistoryKind, HistoryRecord


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
        self.unassigned = SpeakerEvidence('unassigned', new_object_id(), '声音归属待定', 'unknown', 'unassigned_audio')

    def associated(self, evidence):
        linked = self.associations.get(evidence.object_id)
        return replace(linked, track_id=evidence.track_id) if linked else evidence

    def canonical(self, profile):
        # Earlier self-introduction parsing could register "L U X 你好".
        # Resolve this specific greeting/name artifact without deleting memories
        # or moving voiceprint registrations between stored profiles.
        return canonical_voice_profile(self.profiles, profile)

    def resolve(self, track_id: str, *, voiceprint_id: str = "", confidence: float | None = None, cluster_id: str = "", uncertain: bool = False) -> SpeakerEvidence:
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
                    if old and old.object_id != evidence.object_id:
                        self.associations[old.object_id] = evidence
                    self.clusters[cluster_id] = evidence
                return evidence
        if uncertain or track_id.startswith(('unidentified-', 'overlap-')):
            return replace(self.unassigned, track_id=track_id)
        if cluster_id:
            if cluster_id not in self.clusters:
                label = f'未命名访客 {len(self.clusters)+1}'
                profile = self.profiles.create(ObjectProfile(new_object_id(), label, status='provisional', source='voice_anonymous'))
                self.clusters[cluster_id] = SpeakerEvidence(track_id, profile.object_id, label, 'unknown', 'anonymous_voice_cluster')
            evidence = replace(self.associated(self.clusters[cluster_id]), track_id=track_id)
            self.tracks[track_id] = evidence
            return evidence
        if track_id not in self.tracks:
            # No stable speaker info: each utterance has a separate track, never
            # reuse the preceding speaker merely because it is the same microphone.
            self.tracks[track_id] = SpeakerEvidence(track_id, new_object_id())
        return self.tracks[track_id]

    def introduce(self, track_id: str, name: str, *, basis: str = 'explicit_self_introduction', cluster_id: str = '', uncertain: bool = False) -> tuple[SpeakerEvidence, str]:
        old = self.resolve(track_id, cluster_id=cluster_id, uncertain=uncertain)
        matches = tuple(p for p in self.profiles.find_by_names(name) if p.status != "rejected")
        if len(matches) > 1:
            return old, "这个称呼对应多个已有对象，需要进一步确认；不要自动关联。"
        profile = matches[0] if matches else self.profiles.create(ObjectProfile(
            object_id=new_object_id(), label=name, status="provisional", source="voice_manual_annotation" if basis == 'manual_selection' else "voice_self_introduction"))
        updated = SpeakerEvidence(track_id, profile.object_id, profile.label,
                                  "introduced", "self_introduction")
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

    def candidate(self, evidence: SpeakerEvidence) -> SpeakerCandidate:
        profile = self.profiles.get(evidence.object_id)
        # This score permits an unknown but referenceable actor to converse;
        # it is not a fabricated voice similarity score.
        return SpeakerCandidate(
            self.subject_id, evidence.object_id, evidence.label,
            aliases=profile.aliases if profile else (), confidence=0.7,
            status="provisional" if evidence.status == "unknown" else (profile.status if profile else "provisional"),
            reason="voice_scene" if evidence.method == "voice_scene" else ("voice_unknown" if evidence.status == "unknown" else evidence.method),
        )


def attach_voiceprint(profiles, object_id: str, voiceprint_id: str) -> None:
    """Attach only after the cloud has successfully registered the sample."""
    profile = profiles.get(object_id)
    if profile is None:
        raise ValueError("unknown object for voiceprint")
    profiles.add_carrier(object_id, CarrierEntry("voiceprint", f"volc:{voiceprint_id}", "volc_registration"))
