import asyncio
import json
import logging
import os
import ssl
import certifi
from typing import Optional

from dotenv import load_dotenv

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

load_dotenv(override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("livekit.agents").setLevel(logging.WARNING)
logging.getLogger("livekit.plugins.google").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

logger = logging.getLogger("agent")

# ── NC model cached at module level — loaded ONCE when worker starts ──────────
# This means zero NC load time per call
_NC_MODEL: Optional[noise_cancellation.BVCTelephony] = None

def _get_nc() -> noise_cancellation.BVCTelephony:
    global _NC_MODEL
    if _NC_MODEL is None:
        _NC_MODEL = noise_cancellation.BVCTelephony()
        logger.info("✅ NC model cached")
    return _NC_MODEL

# Pre-load NC at import time so it's ready before any call arrives
try:
    _get_nc()
except Exception:
    pass


async def _log(level: str, msg: str, detail: str = "") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def load_db_settings_to_env() -> None:
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
        logger.warning("Could not load DB settings: %s", exc)


_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
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


def _build_session(tools: list, system_prompt: str) -> AgentSession:
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    gemini_voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"
    RealtimeClass = _google_realtime or (_google_beta_realtime if use_realtime else None)

    if use_realtime and RealtimeClass is not None:
        logger.info("🎙  Gemini Live | model=%s voice=%s", gemini_model, gemini_voice)
        _rt = _sr = _cw = None
        try:
            from google.genai import types as _gt
            _rt = _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=2000,
                    prefix_padding_ms=200,
                ),
            )
            _sr = _gt.SessionResumptionConfig(transparent=True)
            _cw = _gt.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=_gt.SlidingWindow(target_tokens=12800),
            )
        except Exception as e:
            logger.warning("Silence-prevention config skipped: %s", e)

        kw: dict = dict(model=gemini_model, voice=gemini_voice, instructions=system_prompt)
        if _rt:
            kw["realtime_input_config"]      = _rt
            kw["session_resumption"]         = _sr
            kw["context_window_compression"] = _cw

        return AgentSession(llm=RealtimeClass(**kw), tools=tools)

    if _google_llm is None:
        raise RuntimeError("No Google AI backend available.")

    logger.info("🎙  Pipeline mode | Deepgram + Gemini + Google TTS")
    stt = _deepgram_stt(model="nova-3", language="multi") if _deepgram_stt else None
    tts = _google_tts() if _google_tts else None
    vad = silero.VAD.load()
    return AgentSession(stt=stt, llm=_google_llm(model=gemini_model), tts=tts, vad=vad, tools=tools)


async def entrypoint(ctx: agents.JobContext) -> None:
    phone_number: Optional[str] = None
    lead_name     = "there"
    business_name = "our company"
    service_type  = "our service"
    custom_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None

    for meta_src in [(ctx.job.metadata if ctx.job else None), ctx.room.metadata]:
        if not meta_src:
            continue
        try:
            d = json.loads(meta_src)
            phone_number     = d.get("phone_number", phone_number)
            lead_name        = d.get("lead_name", lead_name)
            business_name    = d.get("business_name", business_name)
            service_type     = d.get("service_type", service_type)
            custom_prompt    = d.get("system_prompt", custom_prompt)
            agent_profile_id = d.get("agent_profile_id", agent_profile_id)
        except Exception:
            pass

    logger.info("📞 Job | phone=%s lead=%s", phone_number, lead_name)

    # ── STEP 1: Load profile + build prompt (no network blocking ops) ─────────
    profile_tools = []
    if agent_profile_id:
        try:
            profile = await get_agent_profile(agent_profile_id)
            if profile:
                if profile.get("voice"): os.environ["GEMINI_TTS_VOICE"] = profile["voice"]
                if profile.get("model"): os.environ["GEMINI_MODEL"]     = profile["model"]
                custom_prompt = custom_prompt or profile.get("system_prompt")
                profile_tools = json.loads(profile.get("enabled_tools", "[]") or "[]")
        except Exception as exc:
            logger.warning("Profile load failed: %s", exc)

    system_prompt = build_prompt(lead_name, business_name, service_type, custom_prompt)
    enabled_tools = profile_tools or await get_enabled_tools()

    # ── STEP 2: Connect to room ───────────────────────────────────────────────
    await ctx.connect()

    tool_ctx = AppointmentTools(ctx, phone_number, lead_name)
    tools    = tool_ctx.build_tool_list(enabled_tools)

    # ── STEP 3: Build + start session (Gemini WS handshake happens here) ──────
    session = _build_session(tools, system_prompt)

    class _Agent(Agent):
        def __init__(self):
            super().__init__(instructions=system_prompt, tools=tools)

    try:
        from livekit.agents import RoomOptions
        await session.start(
            room=ctx.room,
            agent=_Agent(),
            room_options=RoomOptions(noise_cancellation=_get_nc()),
        )
    except Exception:
        await session.start(
            room=ctx.room,
            agent=_Agent(),
            room_input_options=RoomInputOptions(noise_cancellation=_get_nc()),
        )

    logger.info("✅ Session ready — dialling now")

    # ── STEP 4: NOW dial — agent is 100% ready before phone rings ────────────
    if phone_number:
        user_present = any(
            "sip_" in p.identity or phone_number.replace("+", "") in p.identity
            for p in ctx.room.remote_participants.values()
        )

        if not user_present:
            trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")
            if not trunk_id:
                logger.error("❌ OUTBOUND_TRUNK_ID not set")
                return

            answered    = asyncio.Event()
            sip_hungup  = asyncio.Event()
            room_closed = asyncio.Event()

            @ctx.room.on("participant_connected")
            def _on_answer(_p):
                logger.info("📲 Answered: %s", phone_number)
                answered.set()

            @ctx.room.on("participant_disconnected")
            def _on_hangup(_p):
                logger.info("📴 Hangup: %s", phone_number)
                sip_hungup.set()

            @ctx.room.on("disconnected")
            def _on_room_close(*_):
                room_closed.set()

            try:
                await ctx.api.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        room_name=ctx.room.name,
                        sip_trunk_id=trunk_id,
                        sip_call_to=phone_number,
                        participant_identity=f"sip_{phone_number.replace('+', '')}",
                        wait_until_answered=False,
                    )
                )
                logger.info("📡 Ringing %s…", phone_number)
            except Exception as exc:
                logger.error("❌ Dial failed: %s", exc)
                await _log("error", f"Dial failed for {phone_number}", str(exc))
                return

            try:
                await asyncio.wait_for(answered.wait(), timeout=45.0)
                # 300ms buffer — audio path fully open, agent speaks instantly
                await asyncio.sleep(0.3)
                logger.info("🗣  Agent speaking…")
            except asyncio.TimeoutError:
                logger.warning("⏱  No answer: %s", phone_number)
                await _log("warning", f"No answer: {phone_number}")
                return

            # Wait for either side to end the call
            await asyncio.wait(
                [
                    asyncio.ensure_future(sip_hungup.wait()),
                    asyncio.ensure_future(room_closed.wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )

            try:
                await ctx.room.disconnect()
            except Exception:
                pass

            logger.info("✅ Call done: %s", phone_number)
            return

    # No phone — just keep alive until room closes
    done = asyncio.Event()

    @ctx.room.on("disconnected")
    def _done(*_): done.set()

    await done.wait()
    logger.info("🏁 Done")


if __name__ == "__main__":
    load_db_settings_to_env()
    init_db()
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
        )
    )
