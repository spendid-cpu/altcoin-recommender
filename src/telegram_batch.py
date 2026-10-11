"""여러 줄 알림을 텔레그램 한 통(길면 여러 통)으로 묶어 보내는 도우미. 주식 후보 알림(src/stock_scan.py)이 쓴다."""

import aiohttp

from src import telegram_client

CHUNK_CHARS = 3500  # 텔레그램 한 통 4096자 제한 안


def chunk(header: str, lines: list[str]) -> list[str]:
    """header를 앞에 붙여 CHUNK_CHARS 안으로 나눈다. 두 번째 통부터는 header 첫 줄에 '(이어서)'를 붙인다."""
    chunks, current = [], []
    size = len(header)
    for line in lines:
        if current and size + len(line) + 2 > CHUNK_CHARS:
            chunks.append(current)
            current, size = [], len(header) + 6
        current.append(line)
        size += len(line) + 2
    if current:
        chunks.append(current)
    out = []
    for i, part in enumerate(chunks):
        head = header if i == 0 else header.split("\n")[0] + " (이어서)"
        out.append(head + "\n\n" + "\n".join(part))
    return out


async def send_all(session: aiohttp.ClientSession, header: str, lines: list[str]) -> None:
    if not lines or not telegram_client.is_configured():
        return
    for text in chunk(header, lines):
        await telegram_client.send_message(session, text)
