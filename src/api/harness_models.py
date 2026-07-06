"""Pydantic models for the ClawCross harness control plane."""

from typing import Any

from pydantic import BaseModel, Field


class HarnessEventRequest(BaseModel):
    user_id: str
    password: str = ""
    action: str = "heartbeat"
    agent_id: str = ""
    agent_type: str = ""
    project_id: str = "default"
    project_title: str = ""
    project_summary: str = ""
    task_id: str = ""
    title: str = ""
    description: str = ""
    status: str = ""
    priority: str = ""
    assignee: str = ""
    due_at: str = ""
    current_task_id: str = ""
    needs_user: bool | None = None
    event_id: str = ""
    event_kind: str = ""
    sequence: int | None = None
    provider: str = ""
    model: str = ""
    session_key: str = ""
    workspace_id: str = ""
    runner_id: str = ""
    endpoint: str = ""
    transport: str = ""
    pid: int | None = None
    host: str = ""
    idle_after_seconds: int | None = None
    secret_id: str = ""
    env_name: str = ""
    required: bool | None = None
    message: str = ""
    summary: str = ""
    comment: str = ""
    kind: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    session_ref: str = ""
    remote_host: str = ""
    worktree: str = ""
    branch: str = ""
    git_sha: str = ""
    run_id: str = ""
    command: str = ""
    exit_code: int | None = None
    log_path: str = ""
    metrics_path: str = ""
    metrics_sha256: str = ""
    started_at: str = ""
    ended_at: str = ""
    verifier: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessOpenCliRunRequest(BaseModel):
    user_id: str
    password: str = ""
    args: list[str] = Field(default_factory=list)
    timeout_seconds: float = 60
    max_output_chars: int = 20000
    profile: str = ""
    allow_mutating: bool = False


class HarnessAcpxProbeRequest(BaseModel):
    user_id: str
    password: str = ""
    provider: str = ""


class HarnessAcpxRuntimeSmokeRequest(BaseModel):
    user_id: str
    password: str = ""
    provider: str = ""
    prompt: str = "Reply OK only."
    session_key: str = ""
    cwd: str = ""
    timeout_sec: int = 45


class HarnessAcpxProviderCoverageRequest(BaseModel):
    user_id: str
    password: str = ""
    providers: list[str] = Field(default_factory=list)
    require_installed: bool = True
    require_enabled: bool = True


class HarnessWorkspaceProvisionRequest(BaseModel):
    user_id: str
    password: str = ""
    workspace_id: str = ""
    backend: str = "isolated"
    base_repo: str = ""
    remote: str = ""
    image: str = ""
    start_container: bool = False
    sandbox_status: str = ""
    agent_server_url: str = ""
    session_api_key_hash: str = ""
    exposed_urls: list[dict[str, Any]] = Field(default_factory=list)


class HarnessWorkspaceDeleteRequest(BaseModel):
    user_id: str
    password: str = ""
    workspace_id: str = ""
    remove_files: bool = False
    archive_before_delete: bool = False


class HarnessSandboxActionRequest(BaseModel):
    user_id: str
    password: str = ""
    workspace_id: str = ""


class HarnessSandboxStartRequest(HarnessSandboxActionRequest):
    command: list[str] = Field(default_factory=list)
    port: int = 0
    health_path: str = "/alive"
    timeout_sec: float = 30
    env: dict[str, str] = Field(default_factory=dict)


class HarnessGitProposalRequest(BaseModel):
    user_id: str
    password: str = ""
    title: str = ""
    body: str = ""
    remote: str = "origin"
    source_branch: str = ""
    target_branch: str = ""
    draft: bool = True
    labels: list[str] = Field(default_factory=list)
    max_diff_chars: int = 200000


class HarnessGitRemoteCreateRequest(HarnessGitProposalRequest):
    token_env: str = ""
    token_secret_ref: str = ""
    allow_remote_write: bool = False
    dry_run: bool = True


class HarnessSecretBindRequest(BaseModel):
    user_id: str
    password: str = ""
    secret_id: str = ""
    env_name: str = ""
    provider: str = ""
    workspace_id: str = ""
    run_id: str = ""
    required: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessSecretDeleteRequest(BaseModel):
    user_id: str
    password: str = ""
    secret_id: str = ""


class HarnessRunEventBatchRequest(BaseModel):
    user_id: str
    password: str = ""
    event_ids: list[str] = Field(default_factory=list)


class HarnessConversationBatchRequest(BaseModel):
    user_id: str
    password: str = ""
    conversation_ids: list[str] = Field(default_factory=list)


class HarnessConversationStartTaskBatchRequest(BaseModel):
    user_id: str
    password: str = ""
    start_task_ids: list[str] = Field(default_factory=list)


class HarnessRunnerHelloRequest(BaseModel):
    user_id: str
    password: str = ""
    runner_id: str = ""
    status: str = "idle"
    endpoint: str = ""
    transport: str = "local"
    pid: int | None = None
    host: str = ""
    host_id: str = ""
    provider: str = ""
    capabilities: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    idle_after_seconds: int = 900
    rotate_runner_token: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessHostRegisterRequest(BaseModel):
    user_id: str
    password: str = ""
    host_id: str = ""
    host_type: str = "managed"
    status: str = "registered"
    provider: str = ""
    runner_id: str = ""
    workspace_id: str = ""
    sandbox_id: str = ""
    endpoint: str = ""
    transport: str = "poll"
    capabilities: list[str] = Field(default_factory=list)
    ttl_seconds: int = 900
    rotate_launch_token: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessHostHelloRequest(BaseModel):
    user_id: str
    password: str = ""
    host_id: str = ""
    host_type: str = "managed"
    status: str = "online"
    provider: str = ""
    runner_id: str = ""
    workspace_id: str = ""
    sandbox_id: str = ""
    endpoint: str = ""
    transport: str = "poll"
    capabilities: list[str] = Field(default_factory=list)
    ttl_seconds: int = 900
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessHostDeleteRequest(BaseModel):
    user_id: str
    password: str = ""
    host_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessRunnerChannelTicketRequest(BaseModel):
    user_id: str
    password: str = ""
    ttl_seconds: int = 60


class HarnessRunnerChannelSessionRequest(BaseModel):
    user_id: str
    password: str = ""
    ttl_seconds: int = 900


class HarnessRunnerChannelSendRequest(BaseModel):
    user_id: str
    password: str = ""
    text: str = ""


class HarnessRunnerReapRequest(BaseModel):
    user_id: str
    password: str = ""
    max_idle_seconds: int = 900
    dry_run: bool = False


class HarnessRunnerFleetPollRequest(BaseModel):
    user_id: str
    password: str = ""
    provider: str = ""
    capability: str = ""
    max_idle_seconds: int = 900
    mark_offline: bool = True
    reap_idle: bool = False
    dry_run: bool = False


class HarnessRunnerCommandPollRequest(BaseModel):
    user_id: str
    password: str = ""
    limit: int = 10
    command_types: list[str] = Field(default_factory=list)


class HarnessRunnerCommandAckRequest(BaseModel):
    user_id: str
    password: str = ""
    status: str = "succeeded"
    result: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessRunnerCommandEventRequest(BaseModel):
    user_id: str
    password: str = ""
    events: list[dict[str, Any]] = Field(default_factory=list)
    heartbeat: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessRunnerSessionSyncRequest(BaseModel):
    user_id: str
    password: str = ""
    command_id: str = ""
    status: str = "running"
    events: list[dict[str, Any]] = Field(default_factory=list)
    heartbeat: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessSessionWaitRequest(BaseModel):
    user_id: str
    password: str = ""
    wait_id: str = ""
    wait_type: str = "human_input"
    status: str = "pending"
    provider: str = ""
    session_key: str = ""
    run_id: str = ""
    workspace_id: str = ""
    runner_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: str = ""


class HarnessAcpxSessionEventRequest(BaseModel):
    user_id: str
    password: str = ""
    event_type: str = "message"
    provider: str = ""
    session_key: str = ""
    run_id: str = ""
    workspace_id: str = ""
    runner_id: str = ""
    cwd: str = ""
    prompt: str = ""
    system_prompt: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    secret_refs: list[str] = Field(default_factory=list)
    return_trace: bool = True
    reset_session: bool = False
    timeout_sec: int | None = None
    ttl_sec: int = 300
    model: str = ""
    max_turns: int | None = None
    approve_all: bool | None = None
    permission_policy: str = ""
    non_interactive_permissions: str = ""
    allowed_tools: str | None = None


class HarnessAgentSpecRunRequest(BaseModel):
    user_id: str
    password: str = ""
    spec_path: str = ""
    spec: dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""
    session_id: str = ""
    session_key: str = ""
    run_id: str = ""
    workspace_id: str = ""
    runner_id: str = ""
    cwd: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    secret_refs: list[str] = Field(default_factory=list)
    return_trace: bool = True
    reset_session: bool = False
    timeout_sec: int | None = None
    ttl_sec: int | None = None
    model: str = ""
    max_turns: int | None = None
    approve_all: bool | None = None
    permission_policy: str = ""
    non_interactive_permissions: str = ""
    allowed_tools: str | None = None
    dry_run: bool = False


class HarnessAgentChildSendRequest(BaseModel):
    user_id: str
    password: str = ""
    agent_name: str = ""
    role: str = ""
    session_id: str = ""
    title: str = ""
    purpose: str = ""
    prompt: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    secret_refs: list[str] = Field(default_factory=list)
    return_trace: bool = True
    reset_session: bool = False
    timeout_sec: int | None = None
    ttl_sec: int | None = None
    model: str = ""
    max_turns: int | None = None
    approve_all: bool | None = None
    permission_policy: str = ""
    non_interactive_permissions: str = ""
    allowed_tools: str | None = None
    dry_run: bool = False


class HarnessSessionMcpToolCallRequest(BaseModel):
    user_id: str
    password: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_sec: float = 30
    dry_run: bool = True


class HarnessSessionMcpToolUpsertRequest(BaseModel):
    user_id: str
    password: str = ""
    tool_name: str = ""
    server_id: str = ""
    source_tool: str = ""
    transport: str = "http"
    config: dict[str, Any] = Field(default_factory=dict)
    inherited: bool = False


class HarnessConversationStartRequest(BaseModel):
    user_id: str
    password: str = ""
    conversation_id: str = ""
    start_task_id: str = ""
    title: str = ""
    provider: str = ""
    session_id: str = ""
    session_key: str = ""
    workspace_id: str = ""
    runner_id: str = ""
    cwd: str = ""
    prompt: str = ""
    system_prompt: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    secret_refs: list[str] = Field(default_factory=list)
    return_trace: bool = True
    timeout_sec: int | None = None
    ttl_sec: int = 300
    model: str = ""
    max_turns: int | None = None
    approve_all: bool | None = None
    permission_policy: str = ""
    non_interactive_permissions: str = ""
    allowed_tools: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    plugins: list[dict[str, Any]] = Field(default_factory=list)
    marketplaces: list[dict[str, Any]] = Field(default_factory=list)
    materialize_marketplaces: bool = False
    marketplace_cache_dir: str = ""
    selected_repository: str = ""
    selected_branch: str = ""
    materialize_selected_repository: bool = False
    repository_cache_dir: str = ""
    agent_type: str = ""
    disabled_skills: list[str] = Field(default_factory=list)
    selected_skills: list[Any] = Field(default_factory=list)
    load_workspace_hooks: bool = False
    run_workspace_setup: bool = False
    workspace_setup_path: str = ".openhands/setup.sh"
    workspace_setup_timeout_sec: int = 300
    preserve_pre_commit_hook: bool = True
    bootstrap_only: bool = False
    start_sandbox_conversation: bool = False
    sync_sandbox_skills: bool = False
    load_public_skills: bool = True
    load_user_skills: bool = True
    load_project_skills: bool = True
    load_org_skills: bool = True
    sandbox_session_api_key: str = ""


class HarnessConversationUpdateRequest(BaseModel):
    user_id: str
    password: str = ""
    title: str | None = None
    public: bool | None = None
    selected_repository: str | None = None
    selected_branch: str | None = None
    git_provider: str | None = None
    metadata: dict[str, Any] | None = None


class HarnessConversationModelRequest(BaseModel):
    user_id: str
    password: str = ""
    provider: str = ""
    model: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    sandbox_session_api_key: str = ""
    timeout_sec: float = 30


class HarnessConversationProfileRequest(BaseModel):
    user_id: str
    password: str = ""
    profile_name: str = ""
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_mode: str = ""
    usage_id: str = ""
    llm: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    sandbox_session_api_key: str = ""
    timeout_sec: float = 30


class HarnessConversationHooksRefreshRequest(BaseModel):
    user_id: str
    password: str = ""
    sandbox_session_api_key: str = ""
    timeout_sec: float = 30


class HarnessConversationWorkspaceArchiveRequest(BaseModel):
    user_id: str
    password: str = ""
    sandbox_session_api_key: str = ""
    archive_path: str = ""
    archive_format: str = "both"
    archive_required: bool = False
    phase: str = "final"
    timeout_sec: float = 120
    max_bytes: int = 512 * 1024 * 1024


class HarnessConversationSendMessageRequest(BaseModel):
    user_id: str
    password: str = ""
    runner_id: str = ""
    delivery: str = ""
    prompt: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    secret_refs: list[str] = Field(default_factory=list)
    return_trace: bool = True
    timeout_sec: int | None = None
    ttl_sec: int = 300
    model: str = ""
    max_turns: int | None = None
    approve_all: bool | None = None
    permission_policy: str = ""
    non_interactive_permissions: str = ""
    allowed_tools: str | None = None
    sandbox_session_api_key: str = ""
    agent_server_run: bool = True


class HarnessConversationPendingMessageRequest(BaseModel):
    user_id: str
    password: str = ""
    runner_id: str = ""
    prompt: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    secret_refs: list[str] = Field(default_factory=list)
    return_trace: bool = True
    timeout_sec: int | None = None
    ttl_sec: int = 300
    model: str = ""
    max_turns: int | None = None
    approve_all: bool | None = None
    permission_policy: str = ""
    non_interactive_permissions: str = ""
    allowed_tools: str | None = None
    max_queue: int = 25
    pending_message_id: str = ""


class HarnessAgentServerReconcileRequest(BaseModel):
    user_id: str
    password: str = ""
    sandbox_session_api_key: str = ""
    include_events: bool = True
    event_limit: int = 500
    timeout_sec: float = 30
