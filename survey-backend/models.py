"""Pydantic models for the Fiber360 Survey API.

Mirrors the TypeScript interfaces found in the mobile app's API client."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Auth ──


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    name: str
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    role: str


class LoginResponse(BaseModel):
    access_token: str
    token: str
    user: UserResponse


# ── Projects ──


class ProjectResponse(BaseModel):
    id: str
    name: str
    region: str
    status: str
    assigned_to: Optional[str] = None
    assigned_user_name: Optional[str] = None
    survey_package_name: Optional[str] = None
    completion: int = 0
    total_assets: int = 0
    lastSync: str = ""
    createdAt: str = ""


class ProjectListResponse(BaseModel):
    items: List[ProjectResponse]


class ProjectStatusUpdate(BaseModel):
    status: str


# ── Survey Data ──


class SurveyFeatureData(BaseModel):
    feature_id: str
    layer_name: str
    display_name: Optional[str] = None
    status: str = "not_started"
    form_data: Dict[str, str] = Field(default_factory=dict)
    notes: Optional[str] = None
    photo_count: int = 0
    measurement_count: int = 0
    surveyed_by: Optional[str] = None
    surveyed_at: Optional[str] = None
    gps_lat: Optional[float] = None
    gps_lng: Optional[float] = None
    gps_accuracy: Optional[float] = None


class SurveyDataSyncRequest(BaseModel):
    features: List[SurveyFeatureData]


class SurveyDataSyncResponse(BaseModel):
    ok: bool = True
    upserted: int = 0


class SurveySubmitRequest(BaseModel):
    features_surveyed: List[Dict[str, Any]] = Field(default_factory=list)
    images: List[Dict[str, Any]] = Field(default_factory=list)
    survey_forms: List[Dict[str, Any]] = Field(default_factory=list)
    completed_at: Optional[str] = None
    notes: Optional[str] = None
    project_id: Optional[str] = None


class SurveySubmitResponse(BaseModel):
    ok: bool = True
    project_id: str
    features_submitted: int = 0
    message: str = "Survey submitted successfully"


# ── Photos / Images ──


class PresignedUrlRequest(BaseModel):
    filename: str
    content_type: str = "image/jpeg"


class PresignedUrlResponse(BaseModel):
    upload_url: str
    image_id: str


class PhotoConfirmRequest(BaseModel):
    category: str = "Evidence"
    latitude: float = 0
    longitude: float = 0
    altitude: Optional[float] = None
    accuracy: Optional[float] = None
    asset_id: Optional[str] = None
    layer_name: Optional[str] = None
    captured_at: Optional[str] = None


class PhotoResponse(BaseModel):
    id: str
    url: str


# ── Team ──


class TeamMemberResponse(BaseModel):
    user_id: str
    name: str
    email: str
    role: str
    joined_at: str


class MessageRequest(BaseModel):
    project_id: str
    content: str
    message_type: str = "text"


class MessageResponse(BaseModel):
    id: int
    project_id: str
    user_id: Optional[str] = None
    user_name: str
    content: str
    message_type: str = "text"
    created_at: str


class ActivityRequest(BaseModel):
    project_id: str
    action: str
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ActivityResponse(BaseModel):
    id: int
    project_id: str
    user_id: Optional[str] = None
    user_name: str
    action: str
    description: str
    metadata: Optional[Dict[str, Any]] = None
    created_at: str


class LocationUpdateRequest(BaseModel):
    project_id: str
    latitude: float
    longitude: float
    accuracy: Optional[float] = None
    timestamp: Optional[str] = None


# ── Measurements ──


class MeasurementData(BaseModel):
    id: Optional[str] = None
    project_id: Optional[str] = None
    feature_id: Optional[str] = None
    label: Optional[str] = None
    fused_value: Optional[float] = None
    final_value: Optional[float] = None
    unit: str = "m"
    confidence: Optional[float] = None
    status: str = "pending"
    sources_json: Optional[List[Any]] = None
    device_fingerprint: Optional[str] = None
    photo_uri: Optional[str] = None
    timestamp: Optional[str] = None


class MeasurementsSyncRequest(BaseModel):
    measurements: List[MeasurementData] = Field(default_factory=list)


class MeasurementsSyncResponse(BaseModel):
    ok: bool = True
    synced: int = 0


class AuditRecordRequest(BaseModel):
    measurement_id: Optional[str] = None
    photo_hash: Optional[str] = None
    depth_map_hash: Optional[str] = None
    measurement_hash: Optional[str] = None
    certificate_hash: Optional[str] = None
    device_fingerprint: Optional[str] = None
    sources_json: str = "[]"
    timestamp: Optional[str] = None


class AuditRecordResponse(BaseModel):
    ok: bool = True
    id: Optional[int] = None


# ── Generic ──


class ErrorResponse(BaseModel):
    detail: str


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "fiber360-survey-api"
    database: str = "connected"
