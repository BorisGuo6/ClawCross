"""
Channel setup catalog for the ClawCross CLI.

The backend (src/api/settings_service.py:CHATBOT_CHANNEL_CATALOG)
already enumerates supported channels, but it only carries the
``env_key`` — the NoneBot adapter reads the value as a JSON array
of bot configs (e.g. ``TELEGRAM_BOTS=[{"token":"..."}]``).

This module adds CLI-side metadata that the backend doesn't need:
setup steps users follow on the upstream platform plus the per-bot
fields we have to collect.  Nothing here is read by the agent server;
it only powers ``clawcross channel setup``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BotField:
    name: str            # JSON key inside one bot entry
    prompt: str          # text shown to the user
    password: bool = False
    default: str = ""
    help: str = ""


@dataclass
class ChannelInfo:
    id: str
    label: str
    env_key: str
    emoji: str = ""
    setup_instructions: list[str] = field(default_factory=list)
    bot_fields: list[BotField] = field(default_factory=list)
    notes: str = ""

    def has_setup(self) -> bool:
        return bool(self.bot_fields)


CHANNELS: dict[str, ChannelInfo] = {
    "telegram": ChannelInfo(
        id="telegram",
        label="Telegram",
        env_key="TELEGRAM_BOTS",
        emoji="📱",
        setup_instructions=[
            "1. Open Telegram and message @BotFather",
            "2. Send /newbot, follow the prompts to name your bot",
            "3. Copy the token BotFather returns",
            "4. (Optional) Get your numeric user ID from @userinfobot — "
            "needed for ALLOWED_USERS later",
        ],
        bot_fields=[
            BotField(name="token", prompt="Bot token", password=True,
                     help="The token @BotFather gave you in step 3."),
            BotField(name="name", prompt="Local bot label",
                     default="bot1",
                     help="Friendly name used in logs/UI. Any short string."),
        ],
    ),
    "discord": ChannelInfo(
        id="discord",
        label="Discord",
        env_key="DISCORD_BOTS",
        emoji="💬",
        setup_instructions=[
            "1. https://discord.com/developers/applications → New Application",
            "2. Bot → Reset Token → copy the token",
            "3. Bot → Privileged Gateway Intents → enable Message Content Intent",
            "4. OAuth2 → URL Generator → check 'bot' + 'applications.commands'",
            "   permissions: Send Messages, Read Message History, Attach Files,",
            "   then open the URL and invite the bot to your server",
        ],
        bot_fields=[
            BotField(name="token", prompt="Bot token", password=True,
                     help="The token from step 2 above."),
            BotField(name="name", prompt="Local bot label",
                     default="bot1"),
        ],
    ),
    "slack": ChannelInfo(
        id="slack",
        label="Slack",
        env_key="SLACK_BOTS",
        emoji="💼",
        setup_instructions=[
            "1. https://api.slack.com/apps → Create New App → From Scratch",
            "2. Socket Mode → Enable → create an App-Level Token "
            "(scope: connections:write) → copy the xapp- token",
            "3. OAuth & Permissions → Bot Token Scopes: chat:write, im:history, "
            "im:write, app_mentions:read, channels:history, channels:join, "
            "files:read, users:read",
            "4. Install App to Workspace → copy the xoxb- bot token",
        ],
        bot_fields=[
            BotField(name="bot_token", prompt="Bot token (xoxb-...)", password=True),
            BotField(name="app_token", prompt="App-level token (xapp-...)", password=True),
            BotField(name="name", prompt="Local bot label", default="bot1"),
        ],
    ),
    "feishu": ChannelInfo(
        id="feishu",
        label="Feishu / Lark",
        env_key="FEISHU_BOTS",
        emoji="🪶",
        setup_instructions=[
            "1. https://open.feishu.cn/app → Create Custom App",
            "2. Credentials & Basic Info → copy App ID and App Secret",
            "3. Bot → Add → set callback URL to /feishu/(your forwarded port)",
            "4. Events & Callbacks → subscribe to im.message.receive_v1",
            "5. Permissions: im:message, im:message.send_as_bot",
        ],
        bot_fields=[
            BotField(name="app_id", prompt="App ID", password=False),
            BotField(name="app_secret", prompt="App Secret", password=True),
            BotField(name="encrypt_key", prompt="Encrypt key (or empty)",
                     password=True, default=""),
            BotField(name="verification_token", prompt="Verification token (or empty)",
                     password=True, default=""),
        ],
    ),
    "dingtalk": ChannelInfo(
        id="dingtalk",
        label="DingTalk",
        env_key="DINGTALK_BOTS",
        emoji="🔔",
        setup_instructions=[
            "1. https://open-dev.dingtalk.com/ → Create app",
            "2. Open Application Info → copy AppKey + AppSecret",
            "3. Enable the bot capability under Capabilities → Bot",
        ],
        bot_fields=[
            BotField(name="app_key", prompt="AppKey"),
            BotField(name="app_secret", prompt="AppSecret", password=True),
        ],
    ),
    "qq": ChannelInfo(
        id="qq",
        label="QQ (Official)",
        env_key="QQ_BOTS",
        emoji="🐧",
        setup_instructions=[
            "1. https://q.qq.com/ → Apply for a QQ bot",
            "2. Copy AppID + Token from the bot's settings",
            "3. (Optional) set QQ_IS_SANDBOX=1 for the sandbox channel",
        ],
        bot_fields=[
            BotField(name="id", prompt="AppID"),
            BotField(name="token", prompt="Token", password=True),
            BotField(name="secret", prompt="AppSecret (or empty)", password=True, default=""),
        ],
    ),
    "webhook": ChannelInfo(
        id="webhook",
        label="Custom Webhook",
        env_key="WEBHOOK_BOTS",
        emoji="🔗",
        setup_instructions=[
            "1. Decide the inbound URL that will POST messages to ClawCross",
            "2. Generate or copy a shared-secret token used by the caller",
            "3. (Optional) configure HMAC verification later via the web UI",
        ],
        bot_fields=[
            BotField(name="name", prompt="Hook label (e.g. mywebhook)", default="hook1"),
            BotField(name="secret", prompt="Shared-secret token", password=True),
        ],
    ),
    "console": ChannelInfo(
        id="console",
        label="Console (local testing)",
        env_key="CONSOLE_BOTS",
        emoji="🖥",
        setup_instructions=[
            "1. No external setup — the console adapter ships with NoneBot.",
            "2. Enable to chat with the bot locally without a real platform.",
        ],
        bot_fields=[
            BotField(name="name", prompt="Console label", default="console"),
        ],
    ),
}


def list_channels() -> list[ChannelInfo]:
    return list(CHANNELS.values())


def get_channel(channel_id: str) -> ChannelInfo | None:
    return CHANNELS.get((channel_id or "").strip().lower())
