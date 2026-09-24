"""Per-call voice pipeline: VAD -> STT -> LLM (+tools) -> TTS, with transcript + recording."""

import asyncio
import io
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndWorkerFrame,
    FunctionCallResultProperties,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport
from pipecat.workers.runner import WorkerRunner

from app.agent_config import AgentConfig
from app.config import Settings
from app.db import CallStore
from app.providers import build_llm, build_stt, build_tts
from app.telephony import Twilio, transfer_twiml

WRAP_UP_MESSAGE = "We're almost out of time for this call. Thank you so much, goodbye!"


@dataclass
class Deps:
    settings: Settings
    store: CallStore
    twilio: Twilio


@dataclass
class CallSession:
    call_id: str
    agent: AgentConfig  # already rendered with call variables
    provider_call_id: str | None = None  # Twilio CallSid; None for browser calls
    transcript: list[dict[str, Any]] = field(default_factory=list)
    end_reason: str | None = None
    transferred: bool = False
    recording_path: str | None = None

    @property
    def is_phone_call(self) -> bool:
        return self.provider_call_id is not None


def _build_tools(session: CallSession, deps: Deps, worker_ref: list[PipelineWorker]):
    """Return (FunctionSchemas, {name: handler}) for the tools the agent enables."""
    schemas: list[FunctionSchema] = []
    handlers: dict[str, Any] = {}

    async def end_call(params: FunctionCallParams):
        session.end_reason = session.end_reason or "agent_ended"
        await params.result_callback(
            {"status": "ending"}, properties=FunctionCallResultProperties(run_llm=False)
        )
        # Queued behind anything still being spoken, so the goodbye is not cut off.
        await worker_ref[0].queue_frames([EndWorkerFrame(reason="end_call")])

    async def transfer_call(params: FunctionCallParams):
        number = session.agent.transfer_number
        if not (session.is_phone_call and number):
            await params.result_callback(
                {"status": "unavailable", "detail": "No human agent is available right now."}
            )
            return
        reason = str(params.arguments.get("reason", ""))
        logger.info(f"[{session.call_id}] transferring to {number}: {reason}")
        session.transferred = True
        session.end_reason = "transferred"
        await params.result_callback(
            {"status": "transferring"}, properties=FunctionCallResultProperties(run_llm=False)
        )
        # Replacing the call's TwiML ends our media stream and bridges the caller.
        await deps.twilio.redirect(
            session.provider_call_id,
            transfer_twiml(number, "Please hold while I connect you."),
        )

    if "end_call" in session.agent.tools:
        schemas.append(
            FunctionSchema(
                name="end_call",
                description=(
                    "Hang up the call. Say a short goodbye first, then call this when the "
                    "conversation is finished or the caller asks to end the call."
                ),
                properties={},
                required=[],
            )
        )
        handlers["end_call"] = end_call

    if "transfer_call" in session.agent.tools:
        schemas.append(
            FunctionSchema(
                name="transfer_call",
                description="Transfer the caller to a human team member.",
                properties={
                    "reason": {
                        "type": "string",
                        "description": "Short summary of why the caller needs a human.",
                    }
                },
                required=["reason"],
            )
        )
        handlers["transfer_call"] = transfer_call

    return schemas, handlers


def _wav_bytes(audio: bytes, sample_rate: int, num_channels: int) -> bytes:
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wf:
            wf.setsampwidth(2)
            wf.setnchannels(num_channels)
            wf.setframerate(sample_rate)
            wf.writeframes(audio)
        return buffer.getvalue()


async def run_call(
    transport: BaseTransport,
    session: CallSession,
    deps: Deps,
    *,
    sample_rate: int | None = None,
) -> None:
    """Run the conversation until either side hangs up, then persist the outcome."""
    agent = session.agent
    started_at = datetime.now(UTC)
    await deps.store.update(session.call_id, status="in_progress", started_at=started_at)

    stt = build_stt(agent, deps.settings)
    llm = build_llm(agent, deps.settings)
    tts = build_tts(agent, deps.settings)

    worker_ref: list[PipelineWorker] = []
    tool_schemas, tool_handlers = _build_tools(session, deps, worker_ref)
    for name, handler in tool_handlers.items():
        llm.register_function(name, handler)

    context = (
        LLMContext(tools=ToolsSchema(standard_tools=tool_schemas)) if tool_schemas else LLMContext()
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    # Stereo recording: caller on the left channel, agent on the right.
    audio_buffer = AudioBufferProcessor(num_channels=2) if agent.record_audio else None

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            *([audio_buffer] if audio_buffer else []),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            **(
                {"audio_in_sample_rate": sample_rate, "audio_out_sample_rate": sample_rate}
                if sample_rate
                else {}
            ),
        ),
    )
    worker_ref.append(worker)
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    async def enforce_max_duration():
        await asyncio.sleep(agent.max_call_duration_secs)
        logger.info(f"[{session.call_id}] max duration reached")
        session.end_reason = session.end_reason or "max_duration"
        await worker.queue_frames([TTSSpeakFrame(WRAP_UP_MESSAGE), EndWorkerFrame()])

    timer: asyncio.Task | None = None

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal timer
        logger.info(f"[{session.call_id}] connected (agent={agent.id})")
        if audio_buffer:
            await audio_buffer.start_recording()
        timer = asyncio.create_task(enforce_max_duration())
        if agent.greeting:
            # Speak the fixed greeting immediately (no LLM round-trip = faster pickup).
            await worker.queue_frames([TTSSpeakFrame(agent.greeting)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"[{session.call_id}] disconnected")
        session.end_reason = session.end_reason or "caller_hangup"
        await runner.cancel()

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):
        session.transcript.append(
            {"role": "user", "text": message.content, "ts": message.timestamp}
        )

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message: AssistantTurnStoppedMessage):
        session.transcript.append(
            {"role": "assistant", "text": message.content, "ts": message.timestamp}
        )

    if audio_buffer:

        @audio_buffer.event_handler("on_audio_data")
        async def on_audio_data(buffer, audio, rate, num_channels):
            if not audio:
                return
            deps.settings.recordings_dir.mkdir(parents=True, exist_ok=True)
            path = deps.settings.recordings_dir / f"{session.call_id}.wav"
            path.write_bytes(_wav_bytes(audio, rate, num_channels))
            session.recording_path = str(path)

    try:
        await runner.run()
    finally:
        if timer:
            timer.cancel()
        ended_at = datetime.now(UTC)
        if session.is_phone_call and not session.transferred:
            # Make sure the PSTN leg is released when the agent ends the conversation.
            await deps.twilio.hang_up(session.provider_call_id)
        await deps.store.update(
            session.call_id,
            status="transferred" if session.transferred else "completed",
            end_reason=session.end_reason or "completed",
            transcript=session.transcript,
            recording_path=session.recording_path,
            ended_at=ended_at,
            duration_secs=(ended_at - started_at).total_seconds(),
        )
        logger.info(f"[{session.call_id}] finished: {session.end_reason}")
