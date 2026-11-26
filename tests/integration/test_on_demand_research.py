
# 미적재 기업 보고서 생성 통합 테스트

import asyncio
import json
import sys

from pathlib import Path
from functools import partial
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.api.on_demand import prepare_on_demand
from fin_research.evidence.registry import EvidenceRegistry
from fin_research.llm.budget import Budget
from fin_research.progress import TerminalProgress
from fin_research.research.graph import build_graph, run_config
from fin_research.research.nodes.researcher import (
    SearchToolCache,
    make_loop_researcher,
)
from fin_research.research.loop.tool_graph import run_tool_research
from fin_research.retrieval.search_target import SearchTarget
from fin_research.tools.doc_search_tool import XMLDocumentSearchTool


# 실제 정형데이터 출처 표시
def on_demand_loop(*args, **kwargs):
    result = run_tool_research(*args, **kwargs)

    for evidence in result.get("evidences", []):
        if evidence.get("source_file") == "fin.dart_financial_accounts":
            evidence["source_file"] = "OpenDART fnlttSinglAcntAll"
            evidence["metadata"].update({
                "source": "OpenDART fnlttSinglAcntAll",
                "source_format": "db",
            })

    return result


async def main():
    company = "CJ대한통운"

    target = SearchTarget(
        "00113410",
        2025,
        "consolidated",
        "20260316001417",
    )

    request = "CJ대한통운의 2025년 연결 기준 재무 상태를 분석해줘."

    # 테스트 실행 중에만 기업 정보 전달
    company_config = ({
        "name": company,
        "corp_code": target.corp_code,
        "reports": {
            str(target.fiscal_year): {
                "rcept_no": target.rcept_no,
            },
        },
    },)

    # 기존에 구현한 미적재 기업 데이터 준비 기능
    with prepare_on_demand(company, target) as data:

        temp_root = data["temp_root"]

        search_tools = SearchToolCache(
            persist_directory=temp_root / "chroma",
        )

        researcher = make_loop_researcher(
            search_tools=search_tools,
            account_tool=data["account_tool"],
            tool_loop_fn=on_demand_loop,
            source_format="xml",
            xml_version=2,
            questions_per_subtopic=1,
            loop_rounds=1,
            progress=TerminalProgress(),
        )

        budget = Budget.from_config()
        registry = EvidenceRegistry()

        # 기존 소스 및 YAML 변경 없이 테스트 조건만 전달
        with (
            patch(
                "fin_research.companies.load_companies",
                return_value=company_config,
            ),
            patch(
                "fin_research.tools.doc_search_tool.XMLDocumentSearchTool",
                new=partial(
                    XMLDocumentSearchTool,
                    project_root=temp_root,
                ),
            ),
        ):
            state = await build_graph(researcher).ainvoke(
                {
                    "request": request,
                    "findings": [],
                },
                run_config(
                    budget,
                    registry,
                    research_rcept_no=target.rcept_no,
                    write_report=True,
                    max_rounds=1,
                ),
            )

        state["evidence_registry"] = [
            item.model_dump(mode="json")
            for item in registry.values()
        ]

        output = PROJECT_ROOT / "data/runs/cj_on_demand_research.json"
        output.parent.mkdir(parents=True, exist_ok=True)

        output.write_text(
            json.dumps(
                state,
                ensure_ascii=False,
                indent=2,
                default=lambda value: value.model_dump(mode="json"),
            ),
            encoding="utf-8",
        )

        print("\n=== 통합 테스트 결과 ===")
        print("보고서 생성:", bool(state.get("report")))
        print("등록된 근거 수:", len(registry.values()))
        print("오류:", state.get("errors", []))

        for finding in state.get("findings", []):
            print(finding.subtopic_id, finding.status)

        print("결과 저장:", output)

    print("임시 폴더 삭제:", not temp_root.exists())


if __name__ == "__main__":
    asyncio.run(main())
