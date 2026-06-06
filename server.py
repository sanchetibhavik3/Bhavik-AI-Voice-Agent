import asyncio
import csv
import io
import json
import logging
import os
import random
import uuid
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(override=False)  # only fills gaps — VPS env vars always win

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-server")

app = FastAPI(title="OutboundAI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── APScheduler ───────────────────────────────────────────────────────────────
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

scheduler = AsyncIOScheduler()


# ── LiveKit API helper ────────────────────────────────────────────────────────

def _lk():
    from livekit import api as lk_api
    return lk_api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL", ""),
        api_key=os.getenv("LIVEKIT_API_KEY", ""),
        api_secret=os.getenv("LIVEKIT_API_SECRET", ""),
    )


async def dispatch_call(
    phone_number: str,
    lead_name: str = "there",
    business_name: str = "our company",
    service_type: str = "our service",
    system_prompt: Optional[str] = None,
    agent_profile_id: Optional[str] = None,
) -> dict:
    """Dispatch a single outbound call via LiveKit agent dispatch."""
    from livekit import api as lk_api

    room_name = f"call-{phone_number.replace('+', '')}-{random.randint(1000, 9999)}"
    metadata = {
        "phone_number": phone_number,
        "lead_name": lead_name,
        "business_name": business_name,
        "service_type": service_type,
    }
    if system_prompt:
        metadata["system_prompt"] = system_prompt
    if agent_profile_id:
        metadata["agent_profile_id"] = agent_profile_id

    client = _lk()
    try:
        dispatch = await client.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name="outbound-caller",
                room=room_name,
                metadata=json.dumps(metadata),
            )
        )
        return {"success": True, "room": room_name, "dispatch_id": dispatch.id}
    finally:
        await client.aclose()


# ── Campaign runner ───────────────────────────────────────────────────────────

async def run_campaign(campaign_id: str) -> None:
    from db import get_campaign, update_campaign_run_stats, log_error

    campaign = await get_campaign(campaign_id)
    if not campaign:
        logger.error("Campaign %s not found", campaign_id)
        return

    try:
        contacts = json.loads(campaign.get("contacts_json", "[]"))
    except Exception:
        contacts = []

    delay = int(campaign.get("call_delay_seconds", 3))
    system_prompt = campaign.get("system_prompt")
    agent_profile_id = campaign.get("agent_profile_id")
    dispatched = 0
    failed = 0

    logger.info("Running campaign %s with %d contacts", campaign_id, len(contacts))

    for contact in contacts:
        phone = contact.get("phone", "").strip()
        if not phone:
            failed += 1
            continue
        try:
            await dispatch_call(
                phone_number=phone,
                lead_name=contact.get("name", "there"),
                business_name=contact.get("business_name", "our company"),
                service_type=contact.get("service_type", "our service"),
                system_prompt=system_prompt,
                agent_profile_id=agent_profile_id,
            )
            dispatched += 1
        except Exception as exc:
            logger.error("Failed to dispatch call to %s: %s", phone, exc)
            await log_error("campaign", f"Failed call to {phone}", str(exc))
            failed += 1

        if delay > 0:
            await asyncio.sleep(delay)

    await update_campaign_run_stats(campaign_id, dispatched, failed)
    logger.info("Campaign %s done: %d dispatched, %d failed", campaign_id, dispatched, failed)


def _schedule_campaign(campaign: dict) -> None:
    """Register a campaign with APScheduler based on its schedule_type."""
    cid = campaign["id"]
    stype = campaign.get("schedule_type", "once")
    stime = campaign.get("schedule_time", "09:00")

    job_id = f"campaign_{cid}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    if stype == "once":
        scheduler.add_job(
            run_campaign, "date",
            run_date=datetime.now(),
            args=[cid],
            id=job_id,
            replace_existing=True,
        )
    elif stype == "daily":
        h, m = (stime.split(":") + ["0"])[:2]
        scheduler.add_job(
            run_campaign, CronTrigger(hour=int(h), minute=int(m)),
            args=[cid], id=job_id, replace_existing=True,
        )
    elif stype == "weekdays":
        h, m = (stime.split(":") + ["0"])[:2]
        scheduler.add_job(
            run_campaign, CronTrigger(day_of_week="mon-fri", hour=int(h), minute=int(m)),
            args=[cid], id=job_id, replace_existing=True,
        )


# ── Pydantic models ───────────────────────────────────────────────────────────

class SingleCallRequest(BaseModel):
    phone_number: str
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "our service"
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class CampaignCreate(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class SettingsSave(BaseModel):
    settings: dict


class CallNotesPatch(BaseModel):
    notes: str


class AgentProfileCreate(BaseModel):
    name: str
    voice: str = "Aoede"
    model: str = "gemini-3.1-flash-live-preview"
    system_prompt: Optional[str] = None
    enabled_tools: list = []
    is_default: bool = False


class AgentProfileUpdate(BaseModel):
    name: Optional[str] = None
    voice: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    enabled_tools: Optional[list] = None
    is_default: Optional[bool] = None


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    path = os.path.join(os.path.dirname(__file__), "ui", "index.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>OutboundAI</h1><p>UI not found. Check ui/index.html</p>")


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    from db import get_stats
    return await get_stats()


# ── Single call ───────────────────────────────────────────────────────────────

@app.post("/api/call/single")
async def single_call(req: SingleCallRequest):
    if not req.phone_number.startswith("+"):
        raise HTTPException(400, "phone_number must start with '+' and country code")
    result = await dispatch_call(
        phone_number=req.phone_number,
        lead_name=req.lead_name,
        business_name=req.business_name,
        service_type=req.service_type,
        system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
    )
    return result


# ── Batch CSV call ────────────────────────────────────────────────────────────

@app.post("/api/call/batch")
async def batch_call(
    file: UploadFile = File(...),
    business_name: str = Query("our company"),
    service_type: str = Query("our service"),
    delay_seconds: int = Query(3),
    agent_profile_id: Optional[str] = Query(None),
):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    contacts = []
    for row in reader:
        phone = (row.get("phone") or row.get("Phone") or row.get("phone_number") or "").strip()
        name = (row.get("name") or row.get("Name") or row.get("lead_name") or "there").strip()
        if phone:
            contacts.append({"phone": phone, "name": name})

    if not contacts:
        raise HTTPException(400, "No valid phone numbers found in CSV. Ensure a 'phone' column exists.")

    dispatched = 0
    failed = 0
    errors = []

    for contact in contacts:
        try:
            await dispatch_call(
                phone_number=contact["phone"],
                lead_name=contact["name"],
                business_name=business_name,
                service_type=service_type,
                agent_profile_id=agent_profile_id,
            )
            dispatched += 1
        except Exception as exc:
            failed += 1
            errors.append({"phone": contact["phone"], "error": str(exc)})

        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    return {"dispatched": dispatched, "failed": failed, "errors": errors[:10]}


# ── Appointments ──────────────────────────────────────────────────────────────

@app.get("/api/appointments")
async def list_appointments(date: Optional[str] = Query(None)):
    from db import get_all_appointments
    return await get_all_appointments(date)


@app.delete("/api/appointments/{appointment_id}")
async def cancel_appointment(appointment_id: str):
    from db import cancel_appointment
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"cancelled": True}


# ── Call logs ─────────────────────────────────────────────────────────────────

@app.get("/api/calls")
async def list_calls(page: int = Query(1), limit: int = Query(20)):
    from db import get_all_calls
    return await get_all_calls(page, limit)


@app.patch("/api/calls/{call_id}/notes")
async def update_notes(call_id: str, body: CallNotesPatch):
    from db import update_call_notes
    ok = await update_call_notes(call_id, body.notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"updated": True}


# ── CRM / Contacts ────────────────────────────────────────────────────────────

@app.get("/api/contacts")
async def list_contacts():
    from db import get_contacts
    return await get_contacts()


@app.get("/api/contacts/{phone}/history")
async def contact_history(phone: str):
    from db import get_calls_by_phone, get_appointments_by_phone, get_contact_memory
    calls = await get_calls_by_phone(phone)
    appointments = await get_appointments_by_phone(phone)
    memories = await get_contact_memory(phone)
    return {"calls": calls, "appointments": appointments, "memories": memories}


# ── Campaigns ─────────────────────────────────────────────────────────────────

@app.get("/api/campaigns")
async def list_campaigns():
    from db import get_all_campaigns
    return await get_all_campaigns()


@app.post("/api/campaigns")
async def create_campaign(req: CampaignCreate):
    from db import create_campaign as db_create
    contacts_json = json.dumps(req.contacts)
    cid = await db_create(
        name=req.name,
        contacts_json=contacts_json,
        schedule_type=req.schedule_type,
        schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds,
        system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
    )
    from db import get_campaign
    campaign = await get_campaign(cid)
    if campaign:
        _schedule_campaign(campaign)
    return {"id": cid}


@app.get("/api/campaigns/{campaign_id}")
async def get_campaign_detail(campaign_id: str):
    from db import get_campaign
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    return campaign


@app.patch("/api/campaigns/{campaign_id}/status")
async def update_campaign_status(campaign_id: str, status: str = Query(...)):
    from db import update_campaign_status
    ok = await update_campaign_status(campaign_id, status)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    return {"updated": True}


@app.delete("/api/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str):
    from db import delete_campaign as db_delete
    job_id = f"campaign_{campaign_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    ok = await db_delete(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    return {"deleted": True}


@app.post("/api/campaigns/{campaign_id}/run")
async def run_campaign_now(campaign_id: str):
    from db import get_campaign
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(run_campaign(campaign_id))
    return {"started": True}


# ── Settings (BYOK) ───────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    from db import get_all_settings
    return await get_all_settings()


@app.post("/api/settings")
async def save_settings(body: SettingsSave):
    from db import save_settings as db_save
    await db_save(body.settings)
    # Apply to current process env
    for k, v in body.settings.items():
        if v:
            os.environ[k] = str(v)
    return {"saved": True}


# ── Error logs / Live logs ────────────────────────────────────────────────────

@app.get("/api/logs")
async def get_logs(
    level: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(200),
):
    from db import get_logs
    return await get_logs(level, source, limit)


@app.delete("/api/logs")
async def clear_logs():
    from db import clear_errors
    await clear_errors()
    return {"cleared": True}


# ── Agent profiles ────────────────────────────────────────────────────────────

@app.get("/api/agent-profiles")
async def list_agent_profiles():
    from db import get_all_agent_profiles
    return await get_all_agent_profiles()


@app.post("/api/agent-profiles")
async def create_agent_profile(req: AgentProfileCreate):
    from db import create_agent_profile as db_create
    pid = await db_create(
        name=req.name,
        voice=req.voice,
        model=req.model,
        system_prompt=req.system_prompt,
        enabled_tools=json.dumps(req.enabled_tools),
        is_default=req.is_default,
    )
    return {"id": pid}


@app.patch("/api/agent-profiles/{profile_id}")
async def update_agent_profile(profile_id: str, req: AgentProfileUpdate):
    from db import update_agent_profile as db_update
    updates = req.model_dump(exclude_none=True)
    if "enabled_tools" in updates:
        updates["enabled_tools"] = json.dumps(updates["enabled_tools"])
    if "is_default" in updates:
        updates["is_default"] = 1 if updates["is_default"] else 0
    ok = await db_update(profile_id, updates)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"updated": True}


@app.delete("/api/agent-profiles/{profile_id}")
async def delete_agent_profile(profile_id: str):
    from db import delete_agent_profile as db_delete
    ok = await db_delete(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"deleted": True}


@app.post("/api/agent-profiles/{profile_id}/default")
async def set_default_profile(profile_id: str):
    from db import set_default_agent_profile
    await set_default_agent_profile(profile_id)
    return {"updated": True}


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    from db import init_db, get_all_campaigns
    init_db()
    scheduler.start()
    # Re-register active campaigns
    try:
        campaigns = await get_all_campaigns()
        for c in campaigns:
            if c.get("status") == "active" and c.get("schedule_type") in ("daily", "weekdays"):
                _schedule_campaign(c)
        logger.info("Loaded %d active campaigns into scheduler", len([c for c in campaigns if c.get("status") == "active"]))
    except Exception as exc:
        logger.warning("Could not load campaigns: %s", exc)


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)
