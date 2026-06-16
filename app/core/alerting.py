import httpx
from loguru import logger

from app.core.config import get_settings

settings = get_settings()


def send_alert(message: str, title: str = "GoodNews AI"):
    """ntfy.sh로 푸시 알림 전송. 실패해도 메인 로직에 영향 없음."""
    try:
        httpx.post(
            f"https://ntfy.sh/{settings.ntfy_topic}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8")},
            timeout=10,
        )
        logger.info(f"ntfy 알림 전송: {title}")
    except Exception as e:
        logger.error(f"ntfy 알림 전송 실패: {e}")
