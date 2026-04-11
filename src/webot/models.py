"""
Pydantic request models for WeBot runtime APIs.
"""

from pydantic import BaseModel, Field


class WeBotSubagentRefRequest(BaseModel):
    user_id: str
    password: str = ""
    agent_ref: str


class WeBotSubagentHistoryRequest(WeBotSubagentRefRequest):
    limit: int = 12


class WeBotToolPolicyUpdateRequest(BaseModel):
    user_id: str
    password: str = ""
    policy: dict


class WeBotSessionRuntimeRequest(BaseModel):
    user_id: str
    password: str = ""
    session_id: str


class WeBotPlanUpdateRequest(WeBotSessionRuntimeRequest):
    title: str
    status: str = "active"
    items: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class WeBotWorkflowPresetApplyRequest(WeBotSessionRuntimeRequest):
    preset_id: str
    metadata: dict = Field(default_factory=dict)


class WeBotTodoUpdateRequest(WeBotSessionRuntimeRequest):
    items: list[dict] = Field(default_factory=list)


class WeBotSessionModeUpdateRequest(WeBotSessionRuntimeRequest):
    mode: str = "execute"
    reason: str = ""


class WeBotSessionInboxDeliverRequest(WeBotSessionRuntimeRequest):
    target_ref: str = ""
    limit: int = 20
    force: bool = False


class WeBotSessionInboxListRequest(WeBotSessionRuntimeRequest):
    target_ref: str = ""
    status: str = "queued"
    limit: int = 20


class WeBotSessionInboxSendRequest(WeBotSessionRuntimeRequest):
    target_ref: str = ""
    body: str = ""


class WeBotRunInterruptRequest(WeBotSessionRuntimeRequest):
    run_id: str = ""
    agent_ref: str = ""


class WeBotBridgeAttachRequest(WeBotSessionRuntimeRequest):
    role: str = "viewer"
    label: str = ""


class WeBotBridgeDetachRequest(BaseModel):
    user_id: str
    password: str = ""
    bridge_id: str


class WeBotVoiceStateUpdateRequest(WeBotSessionRuntimeRequest):
    enabled: bool = False
    auto_read_aloud: bool = False
    last_transcript: str = ""
    tts_model: str = ""
    tts_voice: str = ""
    stt_model: str = ""


class WeBotBuddyActionRequest(BaseModel):
    user_id: str
    password: str = ""
    session_id: str = ""
    action: str = "pet"


class WeBotKairosUpdateRequest(WeBotSessionRuntimeRequest):
    enabled: bool = False
    reason: str = ""


class WeBotDreamRequest(WeBotSessionRuntimeRequest):
    reason: str = ""


class WeBotVerificationCreateRequest(WeBotSessionRuntimeRequest):
    title: str
    status: str = "passed"
    details: str = ""


class WeBotApprovalResolutionRequest(BaseModel):
    user_id: str
    password: str = ""
    approval_id: str
    action: str = "approve"
    reason: str = ""
    remember: bool = False
    session_id: str = ""
