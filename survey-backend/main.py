"""Fiber360 Survey API — FastAPI backend for the Fiber360 mobile survey app.

Provides auth, project management, survey data sync, photo upload, team
collaboration, and measurement endpoints. Shares the PostGIS database with
the FTTH HLD planning engine via the `business` schema.

All endpoints are prefixed under /api/v1/ to match the mobile app's API client.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    Depends,
    Query,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import auth as auth_module
import database as db
from models import (
    ActivityRequest,
    ActivityResponse,
    HealthResponse,
    LoginRequest,
    LoginResponse,
    MessageRequest,
    MessageResponse,
    MeasurementsSyncRequest,
    MeasurementsSyncResponse,
    PhotoConfirmRequest,
    PresignedUrlRequest,
    PresignedUrlResponse,
    ProjectListResponse,
    ProjectResponse,
    ProjectStatusUpdate,
    RegisterRequest,
    SurveyDataSyncRequest,
    SurveyDataSyncResponse,
    SurveySubmitRequest,
    SurveySubmitResponse,
    UserResponse,
)

# ── App setup ──

app = FastAPI(
    title="Fiber360 Survey API",
    version="1.0.0",
    description="Backend for the Fiber360 mobile survey application.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

APP_STARTED_AT = datetime.now(timezone.utc)


# ── Startup ──


@app.on_event("startup")
def startup() -> None:
    if db.is_available():
        auth_module.init_users_table()
        db.init_survey_schema()
    else:
        print("[SurveyAPI] WARNING: PostGIS not available at startup")


# ── Health ──


@app.get("/health", response_model=HealthResponse)
def health():
    db_ok = db.is_available()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        database="connected" if db_ok else "unavailable",
    )


# ── Auth Endpoints ──


@app.post("/api/v1/auth/login", response_model=LoginResponse)
def login(req: LoginRequest):
    user = auth_module.get_user_by_email(req.email)
    if not user or not auth_module.verify_password(req.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = auth_module.create_access_token({"sub": user["id"], "email": user["email"]})
    return LoginResponse(
        access_token=token,
        token=token,
        user=UserResponse(
            id=user["id"],
            email=user["email"],
            name=user["name"],
            role=user["role"],
        ),
    )


@app.post("/api/v1/auth/register", response_model=LoginResponse)
def register(req: RegisterRequest):
    user_data = auth_module.create_user(
        email=req.email,
        name=req.name,
        password=req.password,
        role="engineer",
    )
    token = auth_module.create_access_token({"sub": user_data["id"], "email": user_data["email"]})
    return LoginResponse(
        access_token=token,
        token=token,
        user=UserResponse(
            id=user_data["id"],
            email=user_data["email"],
            name=user_data["name"],
            role=user_data["role"],
        ),
    )


@app.get("/api/v1/auth/me", response_model=UserResponse)
def get_me(current_user: Dict = Depends(auth_module.get_current_user)):
    return UserResponse(
        id=current_user["id"],
        email=current_user["email"],
        name=current_user["name"],
        role=current_user["role"],
    )


# ── Project Endpoints ──


@app.get("/api/v1/projects/", response_model=ProjectListResponse)
def list_projects(
    limit: int = Query(50, ge=1, le=200),
    current_user: Dict = Depends(auth_module.get_current_user),
):
    projects = db.list_survey_projects(limit=limit)
    return ProjectListResponse(items=projects)


@app.get("/api/v1/projects/assigned-to-me", response_model=ProjectListResponse)
def list_assigned_projects(
    limit: int = Query(50, ge=1, le=200),
    current_user: Dict = Depends(auth_module.get_current_user),
):
    projects = db.list_assigned_projects(current_user["id"], limit=limit)
    return ProjectListResponse(items=projects)


@app.get("/api/v1/projects/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    project = db.get_survey_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.patch("/api/v1/projects/{project_id}/accept", response_model=ProjectResponse)
def accept_project(
    project_id: str,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    project = db.accept_project(project_id, current_user["id"], current_user["name"])
    if not project or not project.get("id"):
        raise HTTPException(status_code=404, detail="Project not found")
    db.log_activity(
        project_id, current_user["id"], current_user["name"],
        "accepted", f"Project accepted by {current_user['name']}",
    )
    return project


@app.patch("/api/v1/projects/{project_id}/status", response_model=ProjectResponse)
def update_project_status(
    project_id: str,
    req: ProjectStatusUpdate,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    try:
        project = db.update_project_status(project_id, req.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not project or not project.get("id"):
        raise HTTPException(status_code=404, detail="Project not found")
    db.log_activity(
        project_id, current_user["id"], current_user["name"],
        "status_change", f"Status changed to {req.status}",
        metadata={"new_status": req.status},
    )
    return project


# ── Survey Data Endpoints ──


@app.post("/api/v1/projects/{project_id}/survey-data", response_model=SurveyDataSyncResponse)
def sync_survey_data(
    project_id: str,
    req: SurveyDataSyncRequest,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    # Ensure project exists (create stub if not)
    project = db.get_survey_project(project_id)
    if not project:
        db.upsert_survey_project(project_id, {"name": f"Project {project_id[:8]}", "status": "ACTIVE"})

    features_data = [f.dict() for f in req.features]
    upserted = db.upsert_survey_entries(project_id, features_data)
    return SurveyDataSyncResponse(ok=True, upserted=upserted)


@app.post("/api/v1/projects/{project_id}/survey-submit", response_model=SurveySubmitResponse)
def submit_survey(
    project_id: str,
    req: SurveySubmitRequest,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    project = db.get_survey_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Mark as SURVEY_COMPLETE
    db.update_project_status(project_id, "SURVEY_COMPLETE")
    db.log_activity(
        project_id, current_user["id"], current_user["name"],
        "survey_submitted", f"Survey completed by {current_user['name']}",
    )
    return SurveySubmitResponse(
        ok=True,
        project_id=project_id,
        features_submitted=len(req.features_surveyed),
        message="Survey submitted successfully",
    )


# ── Image / Photo Endpoints ──


@app.post("/api/v1/projects/{project_id}/images/upload")
async def upload_image(
    project_id: str,
    file: UploadFile = File(...),
    category: str = Form("Evidence"),
    latitude: float = Form(0.0),
    longitude: float = Form(0.0),
    altitude: Optional[float] = Form(None),
    accuracy: Optional[float] = Form(None),
    asset_id: Optional[str] = Form(None),
    layer_name: Optional[str] = Form(None),
    display_name: Optional[str] = Form(None),
    current_user: Dict = Depends(auth_module.get_current_user),
):
    # Save file locally
    photo_id = f"photo_{uuid.uuid4().hex[:12]}"
    ext = Path(file.filename or "image.jpg").suffix or ".jpg"
    filename = f"{photo_id}{ext}"
    file_path = UPLOAD_DIR / filename

    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    db.save_photo({
        "id": photo_id,
        "project_id": project_id,
        "feature_id": asset_id,
        "layer_name": layer_name,
        "category": category,
        "file_path": str(file_path),
        "original_filename": file.filename or filename,
        "latitude": latitude,
        "longitude": longitude,
        "altitude": altitude,
        "accuracy": accuracy,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "uploaded": True,
    })

    return {"id": photo_id, "url": f"/api/v1/projects/{project_id}/images/{photo_id}/file"}


@app.post("/api/v1/projects/{project_id}/images/presigned-url", response_model=PresignedUrlResponse)
def request_presigned_url(
    project_id: str,
    req: PresignedUrlRequest,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    """Generate a presigned upload URL (local implementation returns a direct upload URL)."""
    image_id = f"img_{uuid.uuid4().hex[:12]}"
    # For local dev: return a direct upload URL (in production, use S3 presigned URLs)
    upload_url = f"/api/v1/projects/{project_id}/images/direct-upload/{image_id}"
    return PresignedUrlResponse(upload_url=upload_url, image_id=image_id)


@app.post("/api/v1/projects/{project_id}/images/{image_id}/confirm")
def confirm_upload(
    project_id: str,
    image_id: str,
    req: PhotoConfirmRequest,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    db.save_photo({
        "id": image_id,
        "project_id": project_id,
        "feature_id": req.asset_id,
        "layer_name": req.layer_name,
        "category": req.category,
        "latitude": req.latitude,
        "longitude": req.longitude,
        "altitude": req.altitude,
        "accuracy": req.accuracy,
        "captured_at": req.captured_at or datetime.now(timezone.utc).isoformat(),
        "uploaded": True,
    })
    return {"ok": True, "id": image_id}


@app.get("/api/v1/projects/{project_id}/images/{image_id}/file")
def get_image_file(project_id: str, image_id: str):
    from fastapi.responses import FileResponse
    file_path = UPLOAD_DIR / f"{image_id}.jpg"
    alt_path = UPLOAD_DIR / image_id
    for p in [file_path, alt_path]:
        if p.exists() and p.is_file():
            return FileResponse(str(p))
    raise HTTPException(status_code=404, detail="Image not found")


# ── Team Endpoints ──


@app.get("/api/v1/projects/{project_id}/team")
def get_team(
    project_id: str,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    from models import TeamMemberResponse
    members = db.get_team_members(project_id)
    return [TeamMemberResponse(**m) for m in members]


# ── Activity Endpoints ──


@app.get("/api/v1/projects/{project_id}/activities")
def get_activities(
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    current_user: Dict = Depends(auth_module.get_current_user),
):
    from models import ActivityResponse
    activities = db.get_activities(project_id, limit=limit)
    return [ActivityResponse(**a) for a in activities]


@app.post("/api/v1/activities")
def log_activity(
    req: ActivityRequest,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    activity_id = db.log_activity(
        project_id=req.project_id,
        user_id=current_user["id"],
        user_name=current_user["name"],
        action=req.action,
        description=req.description or "",
        metadata=req.metadata,
    )
    return {"ok": True, "id": activity_id}


# ── Message Endpoints ──


@app.get("/api/v1/projects/{project_id}/messages")
def get_messages(
    project_id: str,
    limit: int = Query(100, ge=1, le=500),
    current_user: Dict = Depends(auth_module.get_current_user),
):
    from models import MessageResponse
    messages = db.get_messages(project_id, limit=limit)
    return [MessageResponse(**m) for m in messages]


@app.post("/api/v1/messages")
def send_message(
    req: MessageRequest,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    msg_id = db.send_message(
        project_id=req.project_id,
        user_id=current_user["id"],
        user_name=current_user["name"],
        content=req.content,
        message_type=req.message_type,
    )
    return {"ok": True, "id": msg_id, "created_at": datetime.now(timezone.utc).isoformat()}


# ── Location Endpoint ──


@app.post("/api/v1/location")
def update_location(
    req: dict,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    """Record a location update. (For now, just acknowledges it.)"""
    return {"ok": True}


# ── Measurement Endpoints ──


@app.post("/api/v1/projects/{project_id}/measurements")
def sync_project_measurements(
    project_id: str,
    req: MeasurementsSyncRequest,
    current_user: Dict = Depends(auth_module.get_current_user),
):
    """Sync measurements for a project."""
    measurements = [m.dict() for m in req.measurements]
    synced = db.upsert_measurements(measurements)
    return {"ok": True, "synced": synced}


@app.post("/api/v1/measurements/sync")
async def sync_measurements(
    measurements_json: str = Form(...),
    current_user: Dict = Depends(auth_module.get_current_user),
):
    """Sync fused measurements (multipart/form-data)."""
    try:
        data = json.loads(measurements_json)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid measurements_json")

    # Accept either a bare array or wrapped in {\"measurements\": [...]}
    if isinstance(data, dict):
        measurements = data.get("measurements", [])
    else:
        measurements = data if isinstance(data, list) else []
    synced = db.upsert_measurements(measurements)
    return MeasurementsSyncResponse(ok=True, synced=synced)


@app.post("/api/v1/measurements/audit")
async def audit_measurement(
    measurement_id: Optional[str] = Form(None),
    photo_hash: Optional[str] = Form(""),
    depth_map_hash: Optional[str] = Form(""),
    measurement_hash: Optional[str] = Form(""),
    certificate_hash: Optional[str] = Form(""),
    device_fingerprint: Optional[str] = Form(""),
    sources_json: str = Form("[]"),
    timestamp: Optional[str] = Form(None),
    current_user: Dict = Depends(auth_module.get_current_user),
):
    record_id = db.save_audit_record({
        "measurement_id": measurement_id,
        "photo_hash": photo_hash,
        "depth_map_hash": depth_map_hash,
        "measurement_hash": measurement_hash,
        "certificate_hash": certificate_hash,
        "device_fingerprint": device_fingerprint,
        "sources_json": json.loads(sources_json) if sources_json else [],
        "timestamp": timestamp,
    })
    return {"ok": True, "id": record_id}


# ── Health check (root) ──


@app.get("/")
def root():
    return {
        "service": "fiber360-survey-api",
        "status": "ok",
        "started_at": APP_STARTED_AT.isoformat(),
        "database": "connected" if db.is_available() else "unavailable",
    }
