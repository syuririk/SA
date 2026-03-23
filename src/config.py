"""설정 관리 클래스."""

import copy
import json
import yaml
from pathlib import Path
from typing import Any, Optional


_DEFAULT_CONFIG = {
    "paths": {
        "drive_base": "/content/drive/MyDrive/Syuririk/001 Project/200 Open Ai Api /study_data",
        "input_dir": "/content",
        "output_dir": None,
    },
    "book": {"index": 0},
    "ocr": {
        "model": "mistral-ocr-latest",
        "batch_size": 50,
        "max_concurrent": 3,
    },
    "chunking": {
        "mode": "auto",
        "model": {"model": "gpt-4.1-nano", "temperature": 0.2},
        "manual_chunks": [],
    },
    "pipeline": {
        "summary": {"model": "gpt-5-mini"},
        "quiz_extract": {"model": "gpt-5-mini"},
        "quiz_create": {"model": "gpt-5-mini"},
        "max_retries": 3,
        "retry_delay": 5,
        "max_concurrent": 5,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """base dict에 override를 재귀적으로 병합."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    """설정 로드/조회/수정/저장을 담당하는 클래스.

    사용법:
        cfg = Config()                      # 기본값으로 생성
        cfg = Config("config.yaml")         # YAML 파일에서 로드
        cfg = Config({"book": {"index": 1}})  # dict로 오버라이드

        # 조회
        cfg.get("paths.drive_base")
        cfg.get("pipeline.summary")         # → {"model": "gpt-5-mini"}

        # 수정
        cfg.set("book.index", 2)
        cfg.set("pipeline.summary.model", "gpt-5.4-mini")

        # 저장
        cfg.save("my_config.yaml")

        # 전체 확인
        cfg.show()
    """

    def __init__(self, source: Any = None):
        self._data = copy.deepcopy(_DEFAULT_CONFIG)
        if source is not None:
            self.load(source)

    # ── 로드 ──────────────────────────────────────

    def load(self, source: Any) -> "Config":
        """YAML 파일 경로(str/Path) 또는 dict에서 설정을 병합."""
        if isinstance(source, (str, Path)):
            path = Path(source)
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    override = yaml.safe_load(f) or {}
                self._data = _deep_merge(self._data, override)
            else:
                print(f"⚠️ 설정 파일 없음: {path} (기본값 사용)")
        elif isinstance(source, dict):
            self._data = _deep_merge(self._data, source)
        else:
            raise TypeError(f"지원하지 않는 소스 타입: {type(source)}")
        return self

    # ── 조회 ──────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """dot-separated 키로 값 조회. 예: 'pipeline.summary.model'"""
        parts = key.split(".")
        node = self._data
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def __getitem__(self, key: str) -> Any:
        result = self.get(key)
        if result is None:
            raise KeyError(key)
        return result

    # ── 수정 ──────────────────────────────────────

    def set(self, key: str, value: Any) -> "Config":
        """dot-separated 키로 값 설정. 중간 경로가 없으면 자동 생성."""
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        return self

    # ── 저장 ──────────────────────────────────────

    def save(self, path: str = "config.yaml") -> Path:
        """현재 설정을 YAML 파일로 저장."""
        p = Path(path)
        with open(p, "w", encoding="utf-8") as f:
            yaml.dump(self._data, f, default_flow_style=False,
                      allow_unicode=True, sort_keys=False)
        print(f"✅ 설정 저장: {p}")
        return p

    # ── 표시 ──────────────────────────────────────

    def show(self, section: Optional[str] = None) -> None:
        """설정 내용을 보기 좋게 출력."""
        data = self.get(section) if section else self._data
        if data is None:
            print(f"⚠️ '{section}' 섹션 없음")
            return
        title = f"⚙️ 설정" + (f" [{section}]" if section else "")
        print(f"\n{'━' * 50}")
        print(title)
        print(f"{'━' * 50}")
        self._print_dict(data)
        print(f"{'━' * 50}\n")

    def _print_dict(self, data: Any, indent: int = 0) -> None:
        prefix = "  " * indent
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    print(f"{prefix}{k}:")
                    self._print_dict(v, indent + 1)
                elif isinstance(v, list) and len(v) > 3:
                    print(f"{prefix}{k}: [{len(v)} items]")
                else:
                    print(f"{prefix}{k}: {v}")
        else:
            print(f"{prefix}{data}")

    # ── 편의 프로퍼티 ─────────────────────────────

    @property
    def drive_base(self) -> str:
        return self.get("paths.drive_base")

    @property
    def output_dir(self) -> str:
        return self.get("paths.output_dir") or self.drive_base

    @property
    def input_dir(self) -> str:
        return self.get("paths.input_dir")

    @property
    def book_index(self) -> int:
        return self.get("book.index", 0)

    @property
    def raw(self) -> dict:
        """전체 설정 dict 반환 (읽기 전용 복사)."""
        return copy.deepcopy(self._data)
