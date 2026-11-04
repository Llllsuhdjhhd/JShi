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
        result = self._module.recall(
            self._query_type(
                subject_id=subject_id,
                query=query,
                object_ids=ids,
                budget_chars=budget_chars,
                person_limit=len(ids),
                general_limit=0,
            )
        )
        entries=list(result.person)
        links=getattr(self,'person_links',None)
        if links is not None:
            for link in links.links(subject_id):
                if link['target'] not in ids: continue
                recalled=self._module.recall(self._query_type(subject_id=subject_id,query=query,
                    object_ids=(link['source'],),budget_chars=budget_chars,person_limit=1,general_limit=0))
                for item in recalled.person:
                    if links.project(subject_id,item.object_id,item.source_event_ids)==link['target']:
                        from dataclasses import replace
                        entries.append(replace(item,object_id=link['target']))
        used=0
        kept=[]
        for item in entries:
            if used+len(item.content)>budget_chars: continue
            kept.append(item);used+=len(item.content)
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
