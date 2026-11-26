"""전체 실행 API: POST /api/research(접수) → GET /api/research/{id}(상태·결과).

작업 상태는 서버 메모리에만 둔다 (재시작하면 사라지고, 서버 프로세스끼리 공유되지 않는다).
임베딩·GPU를 함께 쓰므로 한 번에 하나씩 실행하고 나머지는 queued로 기다린다.

모든 작업은 프로세스에 하나뿐인 전용 이벤트 루프에서 실행한다. langchain_openai가 비동기 연결을
프로세스 안에서 재사용하므로, 작업마다 새 루프를 만들면 "Event loop is closed"가 난다.
"""
from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from datetime import datetime, timezone
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException

from fin_research.api.schemas import ResearchAcceptedResponse, ResearchJobResponse, ResearchRequest

if TYPE_CHECKING:
    from fin_research.api.research_service import ResearchService

MAX_JOBS = 100  # 끝난 작업은 이 개수까지만 보관
MAX_ACTIVE = 10  # 대기·실행 중 작업 상한


class TooManyJobs(RuntimeError):
    pass


@lru_cache(maxsize=1)
def background_loop() -> asyncio.AbstractEventLoop:
    """조사 작업 전용 이벤트 루프 (데몬 스레드). 서버의 요청 처리를 막지 않는다."""
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="research-loop", daemon=True).start()
    return loop


def run_in_background(coroutine: Coroutine) -> Future[Any]:
    return asyncio.run_coroutine_threadsafe(coroutine, background_loop())


def default_service() -> ResearchService:
    """임베딩·그래프 모듈은 첫 조사 때 불러온다 (서버 시작과 업로드 API는 가볍게 유지)."""
    from fin_research.api.research_service import ResearchService

    return ResearchService()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchJobs:
    """메모리 작업 목록과 실행기."""

    def __init__(self, service_factory: Callable[[], ResearchService] = default_service, workers: int = 1):
        self._factory = service_factory
        self._slots = asyncio.Semaphore(workers)
        self._jobs: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def submit(self, request: str, corp_code: str | None = None) -> str:
        job_id = str(uuid4())
        with self._lock:
            active = sum(j["status"] in ("queued", "running") for j in self._jobs.values())
            if active >= MAX_ACTIVE:
                raise TooManyJobs
            for old in [k for k, j in self._jobs.items() if j["status"] not in ("queued", "running")]:
                if len(self._jobs) < MAX_JOBS:
                    break
                del self._jobs[old]
            self._jobs[job_id] = {"id": job_id, "status": "queued", "stage": None, "result": None,
                                  "created_at": _now(), "finished_at": None}
        run_in_background(self._run(job_id, request, corp_code))
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def _update(self, job_id: str, **values) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    async def _run(self, job_id: str, request: str, corp_code: str | None) -> None:
        async with self._slots:
            self._update(job_id, status="running")
            try:
                service = await asyncio.to_thread(self._factory)  # 첫 실행의 모듈 로딩이 오래 걸린다
                result = await service.run(
                    request, corp_code=corp_code, on_stage=lambda stage: self._update(job_id, stage=stage))
                self._update(job_id, status=result["status"], result=result, finished_at=_now())
            except Exception as e:  # 서비스가 처리하지 못한 오류도 작업 상태로 남긴다
                self._update(job_id, status="failed", finished_at=_now(),
                             result={"error": {"code": "internal_error", "stage": None, "message": type(e).__name__}})


def create_router(jobs: ResearchJobs) -> APIRouter:
    router = APIRouter()

    @router.post("/api/research", response_model=ResearchAcceptedResponse, status_code=202)
    def create_research(body: ResearchRequest):
        """자연어 요청을 접수한다."""
        try:
            job_id = jobs.submit(body.request, body.corp_code)
        except TooManyJobs:
            raise HTTPException(429, "대기 중인 조사 작업이 많습니다. 잠시 후 다시 요청하세요.") from None
        return ResearchAcceptedResponse(id=job_id, status="queued")

    @router.get("/api/research/{job_id}", response_model=ResearchJobResponse)
    def get_research(job_id: UUID):
        """진행 상태와 결과를 조회한다."""
        job = jobs.get(str(job_id))
        if job is None:
            raise HTTPException(404, "조사 작업을 찾을 수 없습니다. 서버가 재시작되면 작업 기록이 사라집니다.")
        return job

    return router
