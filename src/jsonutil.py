"""JSON 저장/전송 헬퍼.

파이썬 json은 NaN/Infinity를 그대로 써 주지만 표준 JSON이 아니라서 브라우저(JSON.parse)가 통째로 읽지 못한다.
거래가 뜸한 종목처럼 스토캐스틱 계산이 0/0이 되는 경우에 NaN이 생기므로, 내보내기 전에 None(null)으로 바꾼다."""

import json
import math


def finite(obj):
    """dict/list를 재귀로 훑어 float NaN/무한대를 None으로 바꾼 사본을 돌려준다."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [finite(v) for v in obj]
    return obj


def dumps(obj, **kwargs) -> str:
    """NaN을 null로 정리한 뒤 엄격한 JSON으로 직렬화한다 (정리 후에도 NaN이 남으면 예외)."""
    return json.dumps(finite(obj), allow_nan=False, **kwargs)
