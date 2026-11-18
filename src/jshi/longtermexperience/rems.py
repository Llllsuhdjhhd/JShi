"""把 REMS 人物经验映射为匠石稳定端口，复用同一记忆数据库。"""

from __future__ import annotations

from typing import Any

from .port import PersonExperience, PersonExperienceRefresh


class RemsLongTermExperience:
    def __init__(self, module: Any, query_type: type) -> None:
        self._module = module
        self._query_type = query_type

    @classmethod
    def from_pipeline(cls, pipeline: Any) -> "RemsLongTermExperience":
        from rems.experience import (
            ExperienceModule,
            ExperienceRecallQuery,
            PersonExperienceRepository,
        )

        module = ExperienceModule.from_repositories(
            pipeline.config,
            pipeline.role_repo,
            PersonExperienceRepository(pipeline.db),
            llm=pipeline.llm,
        )
        return cls(module, ExperienceRecallQuery)

    def recall_experience(
        self,
        subject_id: str,
        query: str,
        *,
        object_ids: tuple[str, ...],
        budget_chars: int = 500,
    ) -> tuple[PersonExperience, ...]:
        ids = tuple(dict.fromkeys(oid for oid in object_ids if oid))
        if not ids or budget_chars <= 0:
            return ()
        links=getattr(self,'person_links',None)
        scope, project = links.recall_scope(subject_id, ids) if links is not None else (ids, None)
        added=set(scope)-set(ids)
        result = self._module.recall(
            self._query_type(
                subject_id=subject_id,
                query=query,
                object_ids=scope,
                budget_chars=budget_chars * (1 + len(added)),
                person_limit=len(scope),
                general_limit=0,
            )
        )
        entries=list(result.person)
        if project is not None:
            from dataclasses import replace
            project=links.projector(subject_id)
            projected=[]
            for item in entries:
                person=project(item.object_id,item.source_event_ids)
                if item.object_id in added and person not in ids: continue
                projected.append(replace(item,object_id=person))
            entries=projected
        used=0
        kept=[]
        seen=set()
        for item in entries:
            key=(item.object_id,item.content,tuple(item.source_event_ids))
            if key in seen: continue
            if used+len(item.content)>budget_chars: continue
            kept.append(item);used+=len(item.content);seen.add(key)
        return tuple(
            PersonExperience(
                object_id=item.object_id,
                content=item.content,
                variant=item.variant,
                source_event_ids=tuple(item.source_event_ids),
                updated_at=item.updated_at,
                score=float(item.score),
            )
            for item in kept
        )

    def refresh_person_experience(
        self, subject_id: str, object_id: str
    ) -> PersonExperienceRefresh:
        result = self._module.refresh_person(subject_id, object_id)
        return PersonExperienceRefresh(
            object_id=result.object_id,
            imported_white_paintings=result.imported_white_paintings,
            updated=result.updated,
            pending_white_paintings=result.pending_white_paintings,
            pending_chars=result.pending_chars,
            review_outcome=result.review_outcome,
        )
