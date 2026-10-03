"""Mock model client — cho unit test chạy không cần API key (mục 9)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class MockModelClient:
    """Định tuyến theo `system` prompt (khớp chuỗi con) -> hàm trả JSON.
    Cho phép test kiểm soát chính xác model sẽ "quyết định" gì ở mỗi bước,
    mà không cần gọi API thật."""

    routes: dict[str, Callable[[str], dict]] = field(default_factory=dict)
    default_response: dict = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    _model_name: str = "mock-model"

    @property
    def model_name(self) -> str:
        return self._model_name

    async def complete_json(
        self, *, system: str, user: str, max_tokens: int = 2000
    ) -> dict:
        self.calls.append((system, user))
        for key, handler in self.routes.items():
            if key in system:
                return handler(user)
        return self.default_response
