"""cquant.ai_advisor.orchestrator — Multi-agent advisor orchestrator."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from cquant.ai_advisor.agents import (
    AgentRole, AgentTurn,
    DebateAgent, ExecutionAgent, ReportWriterAgent, ResearchAgent, RiskAgent,
)
from cquant.ai_advisor.router import IntentRouter
from cquant.ai_advisor.context import RAGContext
from cquant.ai_advisor.policies import SafetyPolicy
from cquant.ai_advisor.providers import LLMProvider
from cquant.ai_advisor.providers.base import run_sync_response, ModelResponse
from cquant.ai_advisor.tools import AdvisorTool, ToolContext
from cquant.core.config import settings
from cquant.datahub.catalog import Catalog
from cquant.knowledge_base import KnowledgeBaseService
from cquant.knowledge_base.store.vector_lance import LanceVectorStore


@dataclass
class AdvisorSession:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    history: list[AgentTurn] = field(default_factory=list)


class AdvisorOrchestrator:
    """Coordinate RAG context, specialist agents, and response synthesis.

    Safety constraints:
    - All tools pass through SafetyPolicy.authorize()
    - Final output passes through SafetyPolicy.validate_response()
    - ExecutionAgent is strictly offline; it cannot reach broker_adapter
    """

    def __init__(
        self,
        provider: LLMProvider,
        agents: Iterable[AgentRole] | None,
        tools: Iterable[AdvisorTool],
        kb_service: KnowledgeBaseService,
        safety: SafetyPolicy,
        *,
        catalog: Catalog | None = None,
        rag: RAGContext | None = None,
    ) -> None:
        self._provider = provider
        self._kb = kb_service
        self._safety = safety
        self._rag = rag or RAGContext()
        self._owns_catalog = catalog is None
        self._catalog = catalog or Catalog(settings.db_path)
        self._catalog.initialize()
        self._tool_registry = {t.name: t for t in tools}

        vector_store = LanceVectorStore(
            Path(settings.storage.knowledge_root) / "vector" / "lancedb"
        )
        self._tool_ctx = ToolContext(
            kb_service=kb_service,
            catalog=self._catalog,
            safety=safety,
            vector_store=vector_store,
        )

        self._agents = (
            {a.role: a for a in agents}
            if agents is not None
            else self._default_agents()
        )
        self._router = IntentRouter()
        self._check_required_agents()

    def close(self) -> None:
        """Close owned resources.

        Closes the catalog DuckDB connection (WAL governance: DuckDB
        checkpoints and removes the .wal file on clean close) — but only when
        this orchestrator created it. A caller-supplied catalog (e.g. the API
        server's shared ``CatalogDep``) remains the caller's responsibility.
        """
        if self._owns_catalog:
            self._catalog.close()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def chat(self, user_message: str, session: AdvisorSession) -> str:
        """Multi-agent Q&A pipeline."""
        session.history.append(AgentTurn(role="user", content=user_message))
        rag_ctx = await asyncio.to_thread(self._rag.build, user_message, self._kb, 2)
        base = f"User message:\n{user_message}\n\nRAG context:\n{rag_ctx}"

        specialist_turns: list[AgentTurn] = []
        research = await self._agents["research"].act(base, session.history)
        specialist_turns.append(research)

        intent = self._router.classify(user_message)
        for role in intent.required_roles:
            if role in self._agents:
                turn = await self._agents[role].act(base, session.history + specialist_turns)
                specialist_turns.append(turn)

        debate = await self._agents["debate"].act(
            _debate_ctx(user_message, specialist_turns),
            session.history + specialist_turns,
        )
        writer = await self._agents["report_writer"].act(
            _writer_ctx(user_message, specialist_turns, debate, report_mode=False),
            session.history + specialist_turns + [debate],
        )

        final = writer.content.strip()
        if not final or "api key is not configured" in final.lower():
            final = _fallback_md("Advisor Response", specialist_turns, debate)

        safe, warning = self._safety.validate_response(final)
        if not safe:
            final = f"{warning}\n\nThe advisor refused to return actionable trading steps."

        session.history.extend(specialist_turns + [debate])
        session.history.append(AgentTurn(
            role="assistant", content=final,
            artifacts=_collect_artifacts(specialist_turns + [debate]),
        ))
        return final

    async def generate_report(self, subject: str, session: AdvisorSession) -> str:
        """Full report pipeline: research → risk → debate → writer."""
        session.history.append(AgentTurn(role="user", content=f"Generate report: {subject}"))
        rag_ctx = await asyncio.to_thread(self._rag.build, subject, self._kb, 3)
        base = f"Report subject:\n{subject}\n\nRAG context:\n{rag_ctx}"

        research = await self._agents["research"].act(base, session.history)
        risk = await self._agents["risk"].act(base, session.history + [research])
        debate = await self._agents["debate"].act(
            _debate_ctx(subject, [research, risk]),
            session.history + [research, risk],
        )
        writer = await self._agents["report_writer"].act(
            _writer_ctx(subject, [research, risk], debate, report_mode=True),
            session.history + [research, risk, debate],
        )

        final = writer.content.strip()
        if not final or "api key is not configured" in final.lower():
            final = _fallback_md("Advisor Report", [research, risk], debate)

        safe, warning = self._safety.validate_response(final)
        if not safe:
            final = f"{warning}\n\nThe report omitted any live-trading guidance."

        session.history.extend([
            research, risk, debate,
            AgentTurn(role="assistant", content=final,
                      artifacts=_collect_artifacts([research, risk, debate])),
        ])
        return final

    def chat_sync(self, user_message: str, session: AdvisorSession) -> str:
        """Sync wrapper for Jupyter / CLI use."""
        async def _wrapped() -> ModelResponse:
            content = await self.chat(user_message, session)
            return ModelResponse(content=content, input_tokens=0, output_tokens=0,
                                 model="advisor_orchestrator", stop_reason="stop")
        return run_sync_response(_wrapped).content

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _default_agents(self) -> dict[str, AgentRole]:
        def tools(*names: str) -> list[AdvisorTool]:
            return [self._tool_registry[n] for n in names if n in self._tool_registry]

        return {
            "research": ResearchAgent(
                self._provider, self._safety, tool_context=self._tool_ctx,
                tools=tools("knowledge_search", "report_summary", "backtest_result", "analysis_report", "ml_prediction", "optimize_guidance", "similar_documents", "entity_relation"),
                max_tokens=2048,
            ),
            "risk": RiskAgent(
                self._provider, self._safety, tool_context=self._tool_ctx,
                tools=tools("risk_snapshot", "alert_status"), max_tokens=1536,
            ),
            "execution": ExecutionAgent(
                self._provider, self._safety, tool_context=self._tool_ctx,
                tools=tools("backtest_run"), max_tokens=1024,
            ),
            "debate": DebateAgent(self._provider, self._safety, max_tokens=1536),
            "report_writer": ReportWriterAgent(self._provider, self._safety, max_tokens=3072),
        }

    def _check_required_agents(self) -> None:
        required = {"research", "risk", "execution", "debate", "report_writer"}
        missing = required - set(self._agents)
        if missing:
            raise ValueError(f"AdvisorOrchestrator missing required agents: {sorted(missing)}")


def _debate_ctx(subject: str, turns: list[AgentTurn]) -> str:
    body = "\n\n".join(f"[{t.role}]\n{t.content}" for t in turns)
    return (f"User request:\n{subject}\n\nSpecialist findings:\n{body}\n\n"
            "Challenge the strongest claims, identify weak evidence, note missing data.")


def _writer_ctx(
    subject: str, specialist: list[AgentTurn], debate: AgentTurn, *, report_mode: bool
) -> str:
    title = "structured markdown report" if report_mode else "concise markdown answer"
    body = "\n\n".join(f"[{t.role}]\n{t.content}" for t in specialist)
    return (
        f"Prepare a {title} for:\n{subject}\n\n"
        f"Specialist findings:\n{body}\n\n"
        f"Devil's advocate review:\n[{debate.role}]\n{debate.content}\n\n"
        "Synthesize findings, cite artifacts, and include uncertainty."
    )


def _fallback_md(title: str, specialist: list[AgentTurn], debate: AgentTurn) -> str:
    sections = [f"# {title}"]
    for t in specialist:
        sections.append(f"## {t.role.replace('_', ' ').title()}\n{t.content}")
    sections.append(f"## Counterpoints\n{debate.content}")
    sections.append(
        "## Safety\nThis advisor is offline-only and cannot access broker adapters or place live trades."
    )
    return "\n\n".join(sections)


def _collect_artifacts(turns: Iterable[AgentTurn]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in turns:
        for a in t.artifacts:
            if a and a not in seen:
                seen.add(a)
                out.append(a)
    return out

