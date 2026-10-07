
"""Bootstrap do container de DI (kink) para arquitetura LLM-first e prompts PT-BR."""
from kink import di
from .settings import Settings
from .logging import get_logger
from .db import create_session_factory
from ..adk.agents.saudacao import AgenteSaudacao
from ..adk.agents.cardapio import AgenteCardapio
from ..adk.agents.carrinho import AgenteCarrinho
from ..adk.agents.endereco import AgenteEndereco
from ..adk.agents.pagamento import AgentePagamento
from .llm_client import LLMClient
from .prompting import PromptBuilder
from .catalog import load_catalog, flatten_for_prompt

def bootstrap_di() -> None:
    settings = Settings()
    di[Settings] = settings
    di["logger"] = get_logger()
    di["session_factory"] = create_session_factory(settings.database_url)
    di[LLMClient] = LLMClient(settings)
    di["catalog"] = load_catalog()
    di["catalog_text"] = flatten_for_prompt(di["catalog"], max_items=120)
    di[PromptBuilder] = PromptBuilder(loja_nome="ADK Burger", janela_coalescencia_ms=settings.coalesce_window_ms)
    # Registro de agentes orientados a prompt
    di["agents"] = {
        "saudacao": AgenteSaudacao,
        "cardapio": AgenteCardapio,
        "carrinho": AgenteCarrinho,
        "endereco": AgenteEndereco,
        "pagamento": AgentePagamento,
    }
