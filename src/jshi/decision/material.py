"""Keep evidence and speaker associations; internal IDs remain in Python."""


def speech_rows(groups, *, bindings=None):
    people = {}
    tracks = {}
    result = []
    source_rows = []
    for group_number, group in enumerate(groups):
        rows = []
        for row in group:
            speaker = row.speaker
            identity = speaker.object_id or speaker.track_id
            code = people.setdefault(identity, 'P' + str(len(people)+1))
            track = tracks.setdefault(speaker.track_id, 'T' + str(len(tracks)+1))
            source_rows.append({'group':group_number,'row':len(rows),'input_id':row.input_id,
                                'person':code,'start_ms':row.start_ms,'end_ms':row.end_ms})
            rows.append({'person': code, 'track':track, 'certainty': speaker.status, 'text': row.text,
                         'audio_seconds': [round(row.start_ms/1000, 2), round(row.end_ms/1000, 2)],
                         'overlap': row.overlap})
        result.append(rows)
    if bindings is not None:
        bindings.update(people={code:ident for ident,code in people.items()},
                        tracks={code:ident for ident,code in tracks.items()}, inputs=source_rows)
    return result
