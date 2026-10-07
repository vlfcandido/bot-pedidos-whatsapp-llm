
"""Orquestrador LLM puro (PT-BR) com PromptBuilder, visão de ferramentas por agente e últimas mensagens."""
from pydantic import BaseModel, Field
from kink import di
import json
from ..core.llm_client import LLMClient
from ..core.prompting import PromptBuilder
from ..core.context import last_messages

class RouterOutput(BaseModel):
    agente_escolhido: str = Field(pattern=r"^(saudacao|cardapio|carrinho|endereco|pagamento)$")
    motivo: str
    acoes_imediatas: list[str] = []
    handoff: bool = False

class Orchestrator:
    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or di[LLMClient]
        self.builder: PromptBuilder = di[PromptBuilder]

    def route(self, contexto: dict, mensagem: str) -> RouterOutput:
        agents = di["agents"]
        # Monta inventário de agentes com lista de tools
        agentes = []
        for nome, agente in agents.items():
            try:
                tools = agente.list_tools() if hasattr(agente, "list_tools") else []
            except Exception:
                tools = []
            agentes.append({
                "nome": nome,
                "objetivo": getattr(agente, "objetivo", ""),
                "tools": [{"name": t.get("function",{}).get("name",""), "description": t.get("function",{}).get("description","" )} for t in tools],
            })
        conversa = last_messages(contexto.get("wa_id",""), limit=5)
        catalog_text = di.get("catalog_text", "")
        system = self.builder.router_system(contexto=contexto | {"catalog_text": catalog_text}, agentes=agentes, conversa=conversa)
        user = f"Mensagem atual do cliente: {mensagem}\nRetorne preferencialmente JSON no schema acordado."
        try:
            out = self.llm.complete_json(system, user, RouterOutput)
            return out
        except Exception:
            # Se não vier JSON: tenta interpretar texto puro como nome do agente
            txt = self.llm.complete_with_tools_loop(system=system, user=user, tools_registry=None, max_steps=0)  # type: ignore
            content = txt.get("content", "saudacao").strip().lower()
            name = "saudacao"
            for cand in ["saudacao","cardapio","carrinho","endereco","pagamento"]:
                if cand in content:
                    name = cand
                    break
            return RouterOutput(agente_escolhido=name, motivo="fallback-text", acoes_imediatas=[], handoff=False)
