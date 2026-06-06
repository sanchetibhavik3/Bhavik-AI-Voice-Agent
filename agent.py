import asyncio
import json
import logging
import os
import ssl
import certifi
from typing import Optional

from dotenv import load_dotenv

# Patch SSL before any network import
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api
from livekit.agents import Agent, AgentSession, RoomInputOptions
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_error, get_enabled_tools, get_agent_profile
from prompts import build_prompt
from tools import AppointmentTools

load_dotenv(override=False)  # only fills gaps — VPS env vars always win
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def load_db_settings_to_env() -> None:
    """Load Supabase settings table into os.environ before worker starts."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            if row.get("value"):
                os.environ[row["key"]] = row["value"]
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


# ── Import Google plugin paths ───────────────────────────────────────────────
_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel (stable path)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel (beta path)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


# ── Session factory ──────────────────────────────────────────────────────────

def _build_session(tools: list, system_prompt: str) -> AgentSession:
    """
    Build AgentSession with Gemini Live or pipeline fallback.

    CRITICAL SILENCE-PREVENTION CONFIG — all 3 required:
    1. SessionResumptionConfig(transparent=True) → auto-reconnects after timeout
    2. ContextWindowCompressionConfig → sliding window prevents token limit freeze
    3. RealtimeInputConfig(END_SENSITIVITY_LOW) → less aggressive VAD, 2s silence threshold

    EndSensitivity MUST use the enum attribute form: _gt.EndSensitivity.END_SENSITIVITY_LOW
    """
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    gemini_voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"

    RealtimeClass = _google_realtime or (_google_beta_realtime if use_realtime else None)

    if use_realtime and RealtimeClass is not None:
        logger.info("SESSION MODE: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
        _realtime_input_cfg = None
        _session_resumption_cfg = None
        _ctx_compression_cfg = None
        try:
            from google.genai import types as _gt
            _realtime_input_cfg = _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=2000,
                    prefix_padding_ms=200,
                ),
            )
            _session_resumption_cfg = _gt.SessionResumptionConfig(transparent=True)
            _ctx_compression_cfg = _gt.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=_gt.SlidingWindow(target_tokens=12800),
            )
            logger.info("Silence-prevention config applied (VAD LOW, transparent resumption, context compression)")
        except Exception as _cfg_err:
            logger.warning("Could not build silence-prevention config: %s", _cfg_err)

        realtime_kwargs: dict = dict(model=gemini_model, voice=gemini_voice, instructions=system_prompt)
        if _realtime_input_cfg is not None:
            realtime_kwargs["realtime_input_config"]      = _realtime_input_cfg
            realtime_kwargs["session_resumption"]         = _session_resumption_cfg
            realtime_kwargs["context_window_compression"] = _ctx_compression_cfg

        return AgentSession(llm=RealtimeClass(**realtime_kwargs), tools=tools)

    if _google_llm is None:
        raise RuntimeError("No Google AI backend. Run: pip install 'livekit-plugins-google>=1.0'")

    logger.info("SESSION MODE: pipeline (Deepgram STT + Gemini LLM + Google TTS)")
    stt = _deepgram_stt(model="nova-3", language="multi") if _deepgram_stt else None
    tts = _google_tts() if _google_tts else None
    vad = silero.VAD.load()
    return AgentSession(stt=stt, llm=_google_llm(model=gemini_model), tts=tts, vad=vad, tools=tools)


# ── Entrypoint ───────────────────────────────────────────────────────────────

async def entrypoint(ctx: agents.JobContext) -> None:
    phone_number: Optional[str] = None
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "our service"
    custom_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None

    # Parse metadata — room metadata takes priority over job metadata
    for meta_src in [
        (ctx.job.metadata if ctx.job else None),
        ctx.room.metadata,
    ]:
        if not meta_src:
            continue
        try:
            data = json.loads(meta_src)
            phone_number     = data.get("phone_number", phone_number)
            lead_name        = data.get("lead_name", lead_name)
            business_name    = data.get("business_name", business_name)
            service_type     = data.get("service_type", service_type)
            custom_prompt    = data.get("system_prompt", custom_prompt)
            agent_profile_id = data.get("agent_profile_id", agent_profile_id)
        except Exception:
            pass

    await _log("info", f"Call entrypoint: phone={phone_number}, lead={lead_name}")

    # Apply agent profile overrides
    if agent_profile_id:
        try:
            profile = await get_agent_profile(agent_profile_id)
            if profile:
                if profile.get("voice"):
                    os.environ["GEMINI_TTS_VOICE"] = profile["voice"]
                if profile.get("model"):
                    os.environ["GEMINI_MODEL"] = profile["model"]
                custom_prompt = custom_prompt or profile.get("system_prompt")
                import json as _j
                profile_tools = _j.loads(profile.get("enabled_tools", "[]") or "[]")
                if profile_tools:
                    await _log("info", f"Agent profile tools override: {profile_tools}")
        except Exception as exc:
            logger.warning("Could not load agent profile %s: %s", agent_profile_id, exc)
            profile_tools = []
    else:
        profile_tools = []

    system_prompt = build_prompt(lead_name, business_name, service_type, custom_prompt)
    tool_ctx = AppointmentTools(ctx, phone_number, lead_name)
    enabled_tools = profile_tools or await get_enabled_tools()
    tools = tool_ctx.build_tool_list(enabled_tools)

    await ctx.connect()

    session = _build_session(tools, system_prompt)

    class _OutboundAgent(Agent):
        def __init__(self):
            super().__init__(instructions=system_prompt, tools=tools)

    await session.start(
        room=ctx.room,
        agent=_OutboundAgent(),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    # Dial out if phone number provided and SIP participant not already in room
    if phone_number:
        user_present = any(
            "sip_" in p.identity or phone_number.replace("+", "") in p.identity
            for p in ctx.room.remote_participants.values()
        )
        if not user_present:
            trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")
            if not trunk_id:
                await _log("error", "OUTBOUND_TRUNK_ID not set — cannot dial", phone_number)
                return
            try:
                await ctx.api.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        room_name=ctx.room.name,
                        sip_trunk_id=trunk_id,
                        sip_call_to=phone_number,
                        participant_identity=f"sip_{phone_number.replace('+', '')}",
                        wait_until_answered=True,
                    )
                )
                await _log("info", f"Call answered: {phone_number}")
            except Exception as exc:
                await _log("error", f"Dial failed for {phone_number}", str(exc))
                return

    # Agent speaks first immediately on connect
    await session.generate_reply(
        instructions=f"The call just connected. Speak first now. Say: 'Hi, am I speaking with {lead_name}?'"
    )


if __name__ == "__main__":
    load_db_settings_to_env()
    init_db()
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
        )
    )
