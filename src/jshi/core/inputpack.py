"""All producers yield text and optional media before the shared JEV entry."""
from dataclasses import asdict, replace
from .envelope import InputPart


def pack_input(envelope, vision=None, subject_id='', *, observation=None):
    if envelope.visual_snapshot_id:
        return envelope
    if observation is None and vision:
        observation = vision.store.latest_sample(subject_id)
    if not observation:
        return envelope
    parts = tuple(p for p in envelope.parts if p.kind != 'image')
    if observation['image_available']:
        parts += (InputPart('image', reference=observation['frame'], media_type='image/jpeg'),)
    return replace(envelope, parts=parts, visual_snapshot_id=observation['id'])


def packet(envelope, vision=None, subject_id=''):
    # Historical scene and prior JEV calls have their own bounded context channel.
    # Do not resend them as part of every current multimodal packet.
    data = {'input_id':envelope.input_id, 'source':envelope.source,
            'parts':[asdict(p) for p in envelope.parts], 'speaker':asdict(envelope.speaker),
            'start_ms':envelope.start_ms, 'end_ms':envelope.end_ms,
            'times':[{'input_id':u.input_id,'start_ms':u.start_ms,'end_ms':u.end_ms,
                      'captured_start_ms':u.recorded_start_at_ms,'captured_end_ms':u.recorded_end_at_ms,
                      'received_ms':u.received_at_ms} for u in envelope.current_utterances]}
    if vision and envelope.visual_snapshot_id:
        data['environment'] = vision.store.snapshot(envelope.visual_snapshot_id, subject_id)
        if vision.config.jev_images:
            import base64
            for part in data['parts']:
                if part['kind'] == 'image':
                    try:
                        photo = vision.store.image(part['reference'], subject_id)
                        part['image_url'] = 'data:image/jpeg;base64,' + base64.b64encode(photo).decode()
                    except KeyError:
                        pass
    return data
