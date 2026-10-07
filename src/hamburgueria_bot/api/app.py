
"""API Flask: webhook Meta, simulate endpoint e controle de handoff (transbordo humano)."""
from __future__ import annotations
from flask import Flask, request, jsonify
from kink import di
from ..core.di import bootstrap_di
from ..core.logging import set_trace_id, get_logger
from ..core.guardrails import sanitize_text
from ..core.catalog import load_catalog, flatten_for_prompt
from ..core.settings import Settings
from ..repo import repo
from ..core.coalesce import coalesce_window
from ..adk.orchestrator import Orchestrator
from ..connectors.whatsapp.cloud_api_adapter import WhatsAppCloudAdapter

app = Flask(__name__)
bootstrap_di()
log = get_logger()

@app.post("/admin/reload-config")
def reload_config():
    """Recarrega o catálogo do disco e atualiza o resumo injetado nos prompts."""
    di["catalog"] = load_catalog()
    di["catalog_text"] = flatten_for_prompt(di["catalog"], max_items=200)
    return {"ok": True, "items_count": sum(len(c.get('items',[])) for c in di['catalog'].get('categories',[]))}


@app.get("/healthz")
def healthz():
    """Health check básico."""
    return {"ok": True}

@app.post("/handoff/pause")
def handoff_pause():
    """Pausa a LLM para um contato (transbordo humano)."""
    body = request.get_json(force=True) or {}
    wa_id = body.get("wa_id")
    reason = body.get("reason", "manual")
    if not wa_id:
        return {"error":"missing wa_id"}, 400
    repo.set_handoff(wa_id, True, reason)
    return {"ok": True, "wa_id": wa_id, "paused": True}

@app.post("/handoff/resume")
def handoff_resume():
    """Retoma a LLM para um contato pausado."""
    body = request.get_json(force=True) or {}
    wa_id = body.get("wa_id")
    if not wa_id:
        return {"error":"missing wa_id"}, 400
    repo.set_handoff(wa_id, False, "resume")
    return {"ok": True, "wa_id": wa_id, "paused": False}

@app.get("/webhook/meta")
def verify():
    """Verificação do webhook: retorna hub.challenge ao validar VERIFY_TOKEN."""
    s = di[Settings]
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == s.verify_token:
        return request.args.get("hub.challenge"), 200
    return "forbidden", 403

@app.post("/webhook/meta")
def webhook():
    """Recebe mensagens, aplica coalescência e orquestra — respeita handoff pausado."""
    set_trace_id(request.headers.get("X-Trace-Id"))
    adapter = WhatsAppCloudAdapter()
    if not adapter.verify_signature(request.data, request.headers.get("X-Hub-Signature-256")):
        return "bad signature", 403

    raw = request.get_json(force=True, silent=False)
    entrada = adapter.normalize_incoming(raw)
    entrada.texto = sanitize_text(entrada.texto)
    log.info("webhook_in", wa_id=entrada.wa_id, provider_id=entrada.provider_message_id)

    # Idempotência (Inbox)
    repo.save_inbox(entrada)

    # Handoff gating
    if repo.get_handoff(entrada.conversation_id):
        repo.log_event(entrada.conversation_id, "handoff_gated", {"provider_message_id": entrada.provider_message_id})
        return jsonify({"queued": False, "reason": "handoff-paused"})

    # Coalescência real
    last_proc = repo.get_last_processed_inbox_id(entrada.conversation_id)
    pacote = coalesce_window(entrada.conversation_id, last_proc)
    repo.log_event(entrada.conversation_id, "coalesce_done", pacote)

    if not pacote["message_ids"]:
        return jsonify({"queued": False, "reason": "no-new-messages"})

    # Contexto
    contexto = repo.load_context(entrada.conversation_id)
    contexto.update({"wa_id": entrada.wa_id})

    # Orquestrar
    rot = Orchestrator().route(contexto=contexto, mensagem=pacote["texto_unificado"])
    repo.log_event(entrada.conversation_id, "router_choice", rot.model_dump())
    if rot.handoff:
        repo.set_handoff(entrada.conversation_id, True, "router_handoff")
        return jsonify({"queued": False, "reason": "handoff-requested"})

    # Executar agente
    response_dict = di["agents"][rot.agente_escolhido].processar(pacote["texto_unificado"], contexto)
    repo.log_event(entrada.conversation_id, "agent_output", {"agent": rot.agente_escolhido, "body": response_dict})

    # Outbox
    repo.enqueue_outbox(entrada.conversation_id, response_dict, source_max_inbox_id=pacote["max_inbox_id"])
    return jsonify({"queued": True, "messages_in_window": len(pacote["message_ids"])})

@app.post("/simulate")
def simulate():
    """Simula uma mensagem sem Meta/assinatura. Útil para desenvolvimento e testes.

    Corpo esperado:
    { "wa_id": "5599999999999", "text": "quero 2 BX2", "provider_message_id": "debug-1" }
    """
    set_trace_id(request.headers.get("X-Trace-Id"))
    body = request.get_json(force=True) or {}
    wa_id = (body.get("wa_id") or "debug-wa")
    texto = sanitize_text(body.get("text",""))
    provider_mid = body.get("provider_message_id") or f"debug-{int(__import__('time').time())}"

    from ..ports.interfaces import MensagemEntradaDTO
    entrada = MensagemEntradaDTO(wa_id=wa_id, provider_message_id=provider_mid, texto=texto, timestamp=int(__import__("time").time()), conversation_id=wa_id)
    repo.save_inbox(entrada)
    log.info("simulate_in", wa_id=wa_id, provider_id=provider_mid, texto=texto)

    # Se estiver pausado, só registra e retorna
    if repo.get_handoff(wa_id):
        repo.log_event(wa_id, "handoff_gated", {"provider_message_id": provider_mid, "simulate": True})
        return jsonify({"preview": None, "reason": "handoff-paused"})

    last_proc = repo.get_last_processed_inbox_id(wa_id)
    pacote = coalesce_window(wa_id, last_proc)
    repo.log_event(wa_id, "coalesce_done", pacote | {"simulate": True})
    if not pacote["message_ids"]:
        return jsonify({"preview": None, "reason": "no-new-messages"})

    contexto = repo.load_context(wa_id)
    contexto.update({"wa_id": wa_id})

    rot = Orchestrator().route(contexto=contexto, mensagem=pacote["texto_unificado"])
    repo.log_event(wa_id, "router_choice", rot.model_dump() | {"simulate": True})
    if rot.handoff:
        repo.set_handoff(wa_id, True, "router_handoff")
        return jsonify({"preview": None, "reason": "handoff-requested"})

    response_dict = di["agents"][rot.agente_escolhido].processar(pacote["texto_unificado"], contexto)
    repo.log_event(wa_id, "agent_output", {"agent": rot.agente_escolhido, "body": response_dict, "simulate": True})

    # Em simulate NÃO enfileiramos; apenas devolvemos a resposta prevista
    return jsonify({"preview": response_dict, "agent": rot.agente_escolhido, "window_msgs": len(pacote["message_ids"]) })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
