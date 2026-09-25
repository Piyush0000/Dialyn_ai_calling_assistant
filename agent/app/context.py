"""Process-wide singletons shared by the HTTP routes and the scheduler."""

from app.agent_config import AgentConfig, load_agent
from app.bot import Deps
from app.calls import CallService
from app.config import get_settings
from app.db import Call, CallStore
from app.ecommerce import build_agent
from app.telephony import Twilio

settings = get_settings()
deps = Deps(settings=settings, store=CallStore(settings.database_url), twilio=Twilio(settings))
service = CallService(deps)
deps.on_call_finished = service.call_finished


async def resolve_agent(call: Call) -> AgentConfig:
    """Merchant order-event calls use templates; everything else uses a YAML agent."""
    if call.event_type and call.tenant_id:
        tenant = await deps.store.get_tenant(call.tenant_id)
        if tenant is None:
            raise LookupError(f"tenant {call.tenant_id} not found")
        return build_agent(tenant, call)
    return load_agent(settings.agents_dir, call.agent_id).render(call.variables)
