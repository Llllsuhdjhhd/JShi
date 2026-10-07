"""Durable JEV scene and unprocessed events, separate from cognition's scene."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path
from threading import RLock
from time import time, monotonic

from jshi.core import SubjectState
from jshi.models import ModelRequest
from jshi.skill.base import parse_json_object
from .jev_prompts import SCENE_INSTRUCTION


class JEVSceneStore:
    def __init__(self, path: Path):
        self.path, self.lock = Path(path), RLock()
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "scene": "", "version": 0, "generation": 0, "sequence": 0,
            "events": [], "covered": [], "codes": {}, "labels": {},
        }

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)
        self.state = state

    def append(self, event_id, payload, *, codes=None, labels=None):
        with self.lock:
            state = deepcopy(self.state)
            if event_id in state["covered"]:
                return
            existing = next((r for r in state["events"] if r["id"] == event_id), None)
            if existing:
                if existing["payload"] != payload:
                    state["generation"] += 1
                    existing["payload"] = deepcopy(payload)
            else:
                state["sequence"] += 1
                state["events"].append({"id": event_id, "seq": state["sequence"], "queued_at": time()*1000, "payload": deepcopy(payload)})
            if codes is not None:
                state["codes"] = dict(codes)
                state["labels"] = dict(labels or {})
            self._save(state)

    def snapshot(self):
        with self.lock:
            return deepcopy(self.state)

    def view(self, cutoff_ms, current_ids=()):
        """An input cannot inherit a scene written using later playback/events."""
        with self.lock:
            scene = ""
            rows = list(self.state["events"])
            for revision in self.state.get("revisions", []):
                if revision["at"] <= cutoff_ms:
                    scene = revision["scene"]
                else:
                    rows.extend(revision["events"])
            current = set(current_ids)
            return scene, [deepcopy(r["payload"]) for r in sorted(rows, key=lambda r:r["seq"])
                           if r["id"] not in current and (r["payload"].get("at") or 0) <= cutoff_ms]

    def correct(self, payload, *, codes=None, labels=None):
        """An in-flight writer cannot restore a scene predating manual corrections."""
        with self.lock:
            state = deepcopy(self.state)
            state["generation"] += 1
            state["version"] += 1
            state["scene"] = ""
            state["revisions"] = []
            self._save(state)
            self.append("correction:" + str(state["generation"]), payload, codes=codes, labels=labels)

    def commit(self, snapshot, scene):
        if not isinstance(scene, str) or not scene.strip() or len(scene) > 1000:
            raise ValueError("JEV片场必须为非空正文且不超过1000字符")
        with self.lock:
            if (snapshot["version"], snapshot["generation"]) != (self.state["version"], self.state["generation"]):
                return False
            ids = {r["id"] for r in snapshot["events"]}
            state = deepcopy(self.state)
            state["scene"] = scene
            state["version"] += 1
            archived = set(snapshot.get("material_window", {}).get("archived_input_ids", []))
            if archived:
                # Full skipped records remain available for analysis after the
                # rolling revision cache expires. Failed/stale writes archive nothing.
                with self.path.with_suffix(".archive.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"version": state["version"], "reason": "recent_material_limit",
                        "events": [r for r in snapshot["events"] if r["id"] in archived]}, ensure_ascii=False) + "\n")
            revisions = state.get("revisions", [])
            revisions.append({"scene": scene, "at": time()*1000, "events": snapshot["events"],
                              "material_window": snapshot.get("material_window", {})})
            state["revisions"] = revisions[-8:]
            state["covered"].extend(r["id"] for r in snapshot["events"])
            state["events"] = [r for r in state["events"] if r["id"] not in ids]
            self._save(state)
            return True


SCENE_SCHEMA = {"type": "object", "properties": {"scene": {"type": "string", "maxLength": 1000}},
                "required": ["scene"], "additionalProperties": False}

RECENT_MATERIAL_CHARS = 3000


def scene_refresh_reason(snapshot, *, idle_seconds, pending_seconds, idle_threshold=15,
                         input_count=6, input_chars=800, max_pending_seconds=30):
    """Fallback refresh when no main turn has triggered a recent scene pass."""
    if not snapshot['events'] or idle_seconds < idle_threshold:
        return ''
    inputs = [r for r in snapshot['events'] if not r['id'].startswith(('played:', 'stopped:'))]
    if len(inputs) >= input_count:
        return 'idle_input_count'
    if sum(len(str(r['payload'].get('text', ''))) for r in inputs) >= input_chars:
        return 'idle_input_chars'
    return 'idle_pending_age' if pending_seconds >= max_pending_seconds else ''


def recent_material(snapshot, main_scene, cap=RECENT_MATERIAL_CHARS):
    """Bound serialized recent dialogue/reference, preserving whole events/blocks.

    The previous compact JEV scene is separate. Retired backlog is archived by
    commit, so it cannot trigger a later pass that overwrites a newer scene.
    """
    selected = []
    reference = []
    def size(rows, blocks):
        return len(json.dumps({"input_output": [r["payload"] for r in rows],
                               "main_scene": "\n".join(blocks)}, ensure_ascii=False, separators=(",", ":")))
    events = snapshot["events"]
    # Reserve only the latest correction and latest confirmation per person;
    # ordinary recent utterances then fill the remaining space.
    anchors, seen = [], set()
    for row in reversed(events):
        payload = row["payload"]
        judgment = payload.get("judgment") or {}
        key = "correction" if row["id"].startswith("correction:") else (
            judgment.get("person") if judgment.get("certainty") in {"确定", "confirmed"} else None)
        if key and key not in seen:
            anchors.append(row)
            seen.add(key)
    # The newest event must get the first chance; anchors cannot crowd out
    # the ongoing conversation with a long list of old confirmed speakers.
    anchor_size = 0
    bounded_anchors = []
    for row in anchors:
        cost = len(json.dumps(row["payload"], ensure_ascii=False, separators=(",", ":")))
        if anchor_size + cost <= cap // 3:
            bounded_anchors.append(row)
            anchor_size += cost
    order = events[-1:] + bounded_anchors + list(reversed(events))
    visited = set()
    for row in order:
        if row["id"] in visited:
            continue
        visited.add(row["id"])
        if size([*selected, row], []) <= cap:
            selected.append(row)
    selected.sort(key=lambda row: row["seq"])
    # Keep whole chronological scene blocks. Never cut a fact mid-sentence.
    blocks = re.split(r"\n(?=B\d+(?:\[|\s))", main_scene.strip()) if main_scene.strip() else []
    for block in reversed(blocks):
        if size(selected, [block, *reference]) <= cap:
            reference.insert(0, block)
    included = {r["id"] for r in selected}
    omitted = [r for r in events if r["id"] not in included]
    snapshot["material_window"] = {
        "cap_chars": cap, "chars": size(selected, reference),
        "model_input_ids": [r["id"] for r in selected],
        "archived_input_ids": [r["id"] for r in omitted],
        "main_scene_chars": len("\n".join(reference)),
        "omitted_main_scene_blocks": len(blocks) - len(reference),
    }
    return [r["payload"] for r in selected], "\n".join(reference)


class JEVSceneWriter:
    def __init__(self, model):
        self.model = model
        self._unavailable_until = 0.0

    def run(self, subject_id, snapshot, main_scene, *, on_request=None):
        if monotonic() < self._unavailable_until:
            raise RuntimeError('JEV片场接口计费或认证失败；60秒冷却期间保留待办')
        # Internal long IDs and progress belong to the store, never to the prompt.
        events, reference = recent_material(snapshot, main_scene)
        user = json.dumps({"scene": snapshot["scene"],
            "input_output": events, "main_scene": reference,
            "window": {"limit": RECENT_MATERIAL_CHARS, "omitted_events": len(snapshot["material_window"]["archived_input_ids"]),
                       "omitted_main_blocks": snapshot["material_window"]["omitted_main_scene_blocks"]},
            "people": {code: snapshot["labels"].get(oid, code) for code, oid in snapshot["codes"].items()}}, ensure_ascii=False, separators=(",", ":"))
        request = ModelRequest(purpose="voice_jev_scene", input_text=user, persona_user_text=user,
            subject_state=SubjectState(subject_id, "匠石", "整理JEV片场"),
            system_extra=SCENE_INSTRUCTION + "\nJSON Schema：" + json.dumps(SCENE_SCHEMA, ensure_ascii=False))
        if on_request:
            on_request(request)
        try:
            raw = self.model.generate(request).text
        except Exception as exc:
            if getattr(exc, 'code', None) in {401, 402, 403}:
                self._unavailable_until = monotonic() + 60
            raise
        snapshot["model_raw"] = raw
        data = parse_json_object(raw)
        scene = data.get("scene")
        if not isinstance(scene, str) or not scene.strip() or len(scene) > 1000:
            raise ValueError("JEV片场无效或超过1000字符；待办保持未处理")
        return scene, raw
