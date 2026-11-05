"""Общее для частей Twitch: адреса (поддельный Twitch тестов — config.TEST_TWITCH), права входа, типы событий
EventSub и ошибки. Нижний модуль пакета: его импортируют все части, сам он — никого из них."""
from .. import config

ID = "https://id.twitch.tv/oauth2"
HELIX = "https://api.twitch.tv/helix"
EVENTSUB = "wss://eventsub.wss.twitch.tv/ws"
if config.TEST_TWITCH:   # поддельный Twitch e2e-тестов (config.TEST_TWITCH)
    ID, HELIX = f"{config.TEST_TWITCH}/oauth2", f"{config.TEST_TWITCH}/helix"
    EVENTSUB = config.TEST_TWITCH.replace("http", "ws", 1) + "/ws"
SCOPES = "channel:manage:redemptions user:read:chat"
# Для ответов зрителю в чат при отказе. Не обязателен: вход, сделанный до его появления, продолжает работать, а
# ответы включаются после повторного входа (интерфейс подсказывает)
REPLY_SCOPE = "user:write:chat"
LOGIN_SCOPES = f"{SCOPES} {REPLY_SCOPE}"
REDEMPTION = "channel.channel_points_custom_reward_redemption.add"
CHAT = "channel.chat.message"


class TwitchError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


class AuthExpired(Exception):
    pass
