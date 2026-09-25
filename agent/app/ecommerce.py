"""E-commerce call templates: turn (merchant, order event, order data) into an agent.

Every event has a goal, the outcomes the agent may record, and an opening line in
English and Hinglish. The agent is only ever told the facts the merchant sent, and
is instructed never to invent anything else.
"""

from dataclasses import dataclass
from typing import Any, Literal

from app.agent_config import AgentConfig, LLMConfig, STTConfig, TTSConfig
from app.db import Call, Tenant

EventType = Literal[
    "order_confirmation",
    "cod_verification",
    "payment_success",
    "payment_failed",
    "order_shipped",
    "out_for_delivery",
]


@dataclass(frozen=True)
class EventTemplate:
    title: str
    goal: str
    outcomes: dict[str, str]  # outcome -> when to use it
    greeting_en: str
    greeting_hi: str


# Outcomes shared by every event.
_COMMON_OUTCOMES = {
    "callback_requested": "The customer is busy and asks to be called later (note the time).",
    "wrong_number": "The person says they did not place this order / wrong person.",
    "needs_human": "The customer has an issue you cannot resolve (complaint, refund, etc.).",
}

TEMPLATES: dict[str, EventTemplate] = {
    "order_confirmation": EventTemplate(
        title="Order confirmation",
        goal=(
            "Confirm that the customer placed this order and that the items and delivery "
            "address are correct."
        ),
        outcomes={
            "confirmed": "Customer confirms the order is correct.",
            "cancel_requested": "Customer wants to cancel the order.",
            "change_requested": "Customer wants to change items, quantity or address (note it).",
        },
        greeting_en=(
            "Hi {customer_name}, this is {agent_name} calling from {brand_name} about your "
            "recent order. Do you have a minute?"
        ),
        greeting_hi=(
            "नमस्ते {customer_name} जी, मैं {brand_name} से {agent_name} बोल रही हूँ, आपके "
            "order के बारे में। क्या अभी एक मिनट बात कर सकते हैं?"
        ),
    ),
    "cod_verification": EventTemplate(
        title="Cash on delivery verification",
        goal=(
            "Verify this cash-on-delivery order before it ships: confirm the customer "
            "ordered it, will pay the amount in cash at delivery, and the address is right. "
            "If they hesitate, you may mention that paying online avoids handling cash, but "
            "never pressure them."
        ),
        outcomes={
            "confirmed": "Customer confirms and will pay cash on delivery.",
            "cancel_requested": "Customer does not want the order any more.",
            "change_requested": "Customer wants to change the address or items (note it).",
            "prepaid_requested": "Customer prefers to pay online instead (payment link).",
        },
        greeting_en=(
            "Hi {customer_name}, this is {agent_name} from {brand_name}. I'm calling to "
            "quickly confirm your cash on delivery order. Is now a good time?"
        ),
        greeting_hi=(
            "नमस्ते {customer_name} जी, मैं {brand_name} से {agent_name} बोल रही हूँ। आपके "
            "cash on delivery order को confirm करना था, क्या अभी बात कर सकते हैं?"
        ),
    ),
    "payment_success": EventTemplate(
        title="Payment successful",
        goal=(
            "Thank the customer, confirm their payment was received for this order and tell "
            "them the expected delivery if known. Answer simple questions using the facts."
        ),
        outcomes={"acknowledged": "Customer heard the update."},
        greeting_en=(
            "Hi {customer_name}, this is {agent_name} from {brand_name}. Good news, we've "
            "received your payment for your order."
        ),
        greeting_hi=(
            "नमस्ते {customer_name} जी, मैं {brand_name} से {agent_name} बोल रही हूँ। आपके "
            "order का payment हमें मिल गया है, धन्यवाद!"
        ),
    ),
    "payment_failed": EventTemplate(
        title="Payment failed",
        goal=(
            "Tell the customer their payment for this order did not go through and help "
            "them complete it: retry with the payment link, or switch to cash on delivery "
            "if they prefer. Reassure them that no money was taken if the facts say so; "
            "otherwise say any deducted amount is usually refunded automatically by the bank."
        ),
        outcomes={
            "will_retry_payment": "Customer will pay again using the payment link.",
            "switch_to_cod": "Customer wants to switch to cash on delivery.",
            "cancel_requested": "Customer no longer wants the order.",
        },
        greeting_en=(
            "Hi {customer_name}, this is {agent_name} from {brand_name}. It looks like the "
            "payment for your order didn't go through. Can I help you complete it?"
        ),
        greeting_hi=(
            "नमस्ते {customer_name} जी, मैं {brand_name} से {agent_name} बोल रही हूँ। आपके "
            "order का payment complete नहीं हो पाया, क्या मैं आपकी मदद कर सकती हूँ?"
        ),
    ),
    "order_shipped": EventTemplate(
        title="Order shipped",
        goal=(
            "Let the customer know their order has shipped, with courier and expected "
            "delivery date if known, and answer simple questions using the facts."
        ),
        outcomes={
            "acknowledged": "Customer heard the update.",
            "change_requested": "Customer needs an address change or has a delivery concern.",
        },
        greeting_en=(
            "Hi {customer_name}, this is {agent_name} from {brand_name}. Your order has been "
            "shipped and is on its way!"
        ),
        greeting_hi=(
            "नमस्ते {customer_name} जी, मैं {brand_name} से {agent_name} बोल रही हूँ। आपका "
            "order ship हो गया है और रास्ते में है!"
        ),
    ),
    "out_for_delivery": EventTemplate(
        title="Out for delivery",
        goal=(
            "Tell the customer the order is out for delivery today and confirm someone will "
            "be available to receive it (and have the cash ready if it is cash on delivery). "
            "If not, capture a preferred delivery day or time."
        ),
        outcomes={
            "will_be_available": "Customer will receive the order today.",
            "reschedule_requested": "Customer wants delivery on another day/time (note it).",
            "address_issue": "Address is wrong or needs directions (note details).",
            "cancel_requested": "Customer refuses the order.",
        },
        greeting_en=(
            "Hi {customer_name}, this is {agent_name} from {brand_name}. Your order is out for "
            "delivery today. Will you be available to receive it?"
        ),
        greeting_hi=(
            "नमस्ते {customer_name} जी, मैं {brand_name} से {agent_name} बोल रही हूँ। आपका "
            "order आज delivery के लिए निकल गया है, क्या आप घर पर मिलेंगे?"
        ),
    ),
}

# Default provider stacks per language (merchants can override via tenant.voice).
# English uses the free stack; Hindi/Hinglish uses Sarvam's Indian voices.
DEFAULT_STACKS: dict[str, dict[str, dict[str, Any]]] = {
    "en": {
        "stt": {"provider": "deepgram", "model": "nova-3", "language": "en"},
        "llm": {"provider": "groq", "model": "qwen/qwen3.8-27b", "temperature": 0.3},
        "tts": {"provider": "deepgram", "voice": "aura-2-thalia-en"},
    },
    "hi": {
        "stt": {"provider": "sarvam", "language": "hi-IN"},
        "llm": {"provider": "groq", "model": "qwen/qwen3.8-27b", "temperature": 0.3},
        "tts": {"provider": "sarvam", "model": "bulbul:v3", "voice": "priya", "language": "hi-IN"},
    },
}

_LANGUAGE_STYLE = {
    "en": "Speak natural, warm English like a friendly Indian customer-care executive.",
    "hi": (
        "Speak natural Hinglish like a friendly Indian customer-care executive: mostly Hindi "
        "written in Devanagari script, keeping common English words (order, payment, "
        "delivery, cash on delivery, address) in English. Use 'aap', be polite."
    ),
}


def _facts(call: Call) -> str:
    order: dict[str, Any] = call.payload.get("order", {})
    lines = []
    if order.get("id"):
        lines.append(f"Order ID: {order['id']}")
    items = order.get("items") or []
    if items:
        described = ", ".join(
            f"{i.get('quantity', 1)} x {i.get('name', 'item')}"
            for i in items
            if isinstance(i, dict)
        )
        lines.append(f"Items: {described}")
    if order.get("amount") is not None:
        lines.append(f"Order amount: {order['amount']} {order.get('currency', 'INR')}")
    labels = {
        "payment_method": "Payment method",
        "address": "Delivery address",
        "expected_delivery": "Expected delivery",
        "courier": "Courier",
        "tracking_number": "Tracking number",
        "payment_link": "Payment link (will be sent by SMS/WhatsApp, never read it aloud)",
    }
    for key, label in labels.items():
        if order.get(key):
            lines.append(f"{label}: {order[key]}")
    return "\n".join(f"- {line}" for line in lines) or "- (no order details provided)"


def build_agent(tenant: Tenant, call: Call) -> AgentConfig:
    """Assemble the agent for one merchant call from its template and order data."""
    template = TEMPLATES[call.event_type]
    language = call.language if call.language in DEFAULT_STACKS else "en"
    values = {
        "customer_name": call.customer_name or "",
        "agent_name": tenant.agent_name,
        "brand_name": tenant.brand_name,
    }
    outcomes = {**template.outcomes, **_COMMON_OUTCOMES}
    outcome_lines = "\n".join(f"- {name}: {when}" for name, when in outcomes.items())

    system_prompt = f"""You are {tenant.agent_name}, a customer-care executive at {tenant.brand_name}, on a live phone call with {call.customer_name or "the customer"}.

Goal of this call: {template.goal}

Order facts (the ONLY facts you know):
{_facts(call)}

How to speak:
- {_LANGUAGE_STYLE[language]}
- Keep every reply to one or two short sentences, ask one question at a time.
- Never use lists, markdown, emojis or symbols. Say amounts naturally, e.g. "one thousand four hundred ninety nine rupees".
- Never invent order details, prices, dates, offers or policies that are not in the facts. If asked something you don't know, say the team will follow up.
- Do not read long IDs or links aloud unless asked; say the link will be sent by SMS or WhatsApp.
- You cannot change orders yourself. For changes, cancellations or new delivery times say you have noted the request and the team will confirm it; never say it is done or guaranteed.

Ending the call:
- As soon as the goal is reached (or clearly cannot be), call record_outcome with one of:
{outcome_lines}
- After recording, say one short thank-you and goodbye sentence; the call then ends automatically.
- If the customer asks for a human, record needs_human and use transfer_call."""

    greeting = template.greeting_hi if language == "hi" else template.greeting_en
    stack = _merge_stack(DEFAULT_STACKS[language], (tenant.voice or {}).get(language, {}))

    return AgentConfig(
        id=f"ecom:{call.event_type}",
        name=f"{tenant.brand_name} — {template.title}",
        language=language,
        system_prompt=system_prompt,
        greeting=greeting.format(**values),
        stt=STTConfig(**stack["stt"]),
        llm=LLMConfig(**stack["llm"]),
        tts=TTSConfig(**stack["tts"]),
        tools=["record_outcome", "end_call", "transfer_call"],
        outcomes=list(outcomes),
        transfer_number=tenant.support_number or "",
        max_call_duration_secs=300,
    )


def _merge_stack(base: dict, override: dict) -> dict:
    return {stage: {**base[stage], **override.get(stage, {})} for stage in ("stt", "llm", "tts")}
