"""Device-facing endpoints of sync protocol v1 (docs/protocol/v1.openapi.yaml)."""
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .. import bundles, devices, items, media, pki
from ..pipeline import dispatch
from .device_auth import check_agent_version, current_device

router = APIRouter()


class HwInfo(BaseModel):
    model: str = Field(max_length=100)
    serial: str = Field(max_length=100)
    agent_version: str = Field(max_length=32)


class PairRequest(BaseModel):
    token: str = Field(min_length=1, max_length=32)
    csr_pem: str = Field(max_length=8192)
    hw_info: HwInfo


class PairResponse(BaseModel):
    device_id: str
    cert_pem: str
    ca_cert_pem: str
    ca_fingerprint: str


@router.post(
    "/v1/pair",
    status_code=201,
    response_model=PairResponse,
    dependencies=[Depends(check_agent_version)],
)
async def pair(body: PairRequest, request: Request) -> PairResponse:
    settings = request.app.state.settings

    def issue(device_id: str) -> str:
        return pki.sign_csr(settings.pki_dir, body.csr_pem, device_id)

    try:
        result = await devices.register_device(
            request.app.state.pool, body.token.strip().upper(), body.hw_info.model_dump(), issue
        )
    except pki.PkiError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=401, detail="pair token invalid, used or expired")
    ca_pem = pki.ca_cert_pem(settings.pki_dir)
    return PairResponse(
        device_id=result["device_id"],
        cert_pem=result["cert_pem"],
        ca_cert_pem=ca_pem,
        ca_fingerprint=pki.fingerprint(ca_pem),
    )


@router.put("/v1/items/{item_id}", dependencies=[Depends(check_agent_version)])
async def put_item(
    item_id: str,
    request: Request,
    background: BackgroundTasks,
    device: dict = Depends(current_device),
) -> JSONResponse:
    settings = request.app.state.settings
    try:
        header = items.parse_header(item_id, request.headers, settings.default_video_fps)
        status, ack = await items.store_item(
            request.app.state.pool, settings, device["id"], header, request.stream()
        )
    except items.ItemRejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    if status == 201:
        # Fast path into the pipeline; the periodic dispatcher catches anything this misses.
        background.add_task(dispatch.defer_item, request.app.state.pool, header.id)
        if header.kind == "video":
            background.add_task(media.remux_item, request.app.state.pool, settings, header.id)
    return JSONResponse(ack, status_code=status)


@router.get("/v1/ota/{bundle}", dependencies=[Depends(check_agent_version)])
async def get_bundle(bundle: str, request: Request, device: dict = Depends(current_device)) -> FileResponse:
    """A signed update bundle for a paired device (P4a design §7). Starlette answers Range requests with 206."""
    ota_dir = request.app.state.settings.ota_dir
    found = bundles.find(ota_dir, bundle)
    if found is None:
        raise HTTPException(status_code=404, detail="unknown bundle")
    return FileResponse(ota_dir / found.name, media_type="application/octet-stream")


ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"
Counter = Annotated[StrictInt, Field(ge=0)]
VERSION_PATTERN = devices.VERSION_RE.pattern
OtaState = Literal["downloading", "installing", "rebooting", "trying", "succeeded", "rolled_back", "failed"]


class OtaReport(BaseModel):
    """Mirrors the contract's OtaReport (P4a design §7)."""

    model_config = ConfigDict(extra="forbid")

    command_id: Annotated[str, Field(pattern=ULID_PATTERN)] | None = None
    state: OtaState
    version: str = Field(pattern=VERSION_PATTERN)
    error: str | None = Field(default=None, max_length=500)


class Heartbeat(BaseModel):
    """Mirrors the contract's Heartbeat schema (additionalProperties: false)."""

    model_config = ConfigDict(extra="forbid")

    agent_version: str = Field(max_length=32)
    uptime_s: Counter
    soc_temp_c: float | None = None
    disk_used_pct: float = Field(ge=0, le=100)
    queue_depth: Counter
    wifi_rssi_dbm: StrictInt | None = None
    clock_synced: StrictBool
    paused: StrictBool
    audio_enabled: StrictBool
    recording_video: StrictBool
    recording_audio: StrictBool
    mic_missing: StrictBool = False
    camera_failures: Counter = 0
    last_error: str | None = Field(default=None, max_length=500)
    dropped_items: Counter = 0
    acked_command_ids: list[Annotated[str, Field(pattern=ULID_PATTERN)]] = Field(max_length=devices.MAX_HEARTBEAT_COMMANDS)
    os_version: str | None = Field(default=None, pattern=VERSION_PATTERN)
    os_variant: Literal["dev", "prod"] | None = None
    boot_slot: Literal["A", "B"] | None = None
    ota: OtaReport | None = None


class Command(BaseModel):
    id: str
    type: Literal["pause", "resume", "set_config", "update"]
    payload: dict


class HeartbeatResponse(BaseModel):
    commands: list[Command]


@router.post(
    "/v1/heartbeat",
    response_model=HeartbeatResponse,
    dependencies=[Depends(check_agent_version)],
)
async def heartbeat(
    body: Heartbeat, request: Request, device: dict = Depends(current_device)
) -> HeartbeatResponse:
    status = body.model_dump(exclude={"acked_command_ids"})
    pending = await devices.record_heartbeat(
        request.app.state.pool, device["id"], status, body.acked_command_ids
    )
    return HeartbeatResponse(commands=[Command(**row) for row in pending])
