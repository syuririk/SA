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
    "quiz_generator": {
        "model": {"model": "gpt-5-mini"},
        "n": 5,
        "quiz_type": "multiple_choice",
        "source": "random",
        "difficulty": "medium",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    """설정 로드/조회/수정/저장.

    사용법:
        cfg = Config()
        cfg = Config("config.yaml")
        cfg = Config({"book": {"index": 1}})

        cfg.get("pipeline.summary")
        cfg.set("book.index", 2)
        cfg.save("my_config.yaml")
        cfg.show()
    """

    def __init__(self, source: Any = None):
        self._data = copy.deepcopy(_DEFAULT_CONFIG)
        if source is not None:
            self.load(source)

    def load(self, source: Any) -> "Config":
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
        return self

    def get(self, key: str, default: Any = None) -> Any:
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

    def set(self, key: str, value: Any) -> "Config":
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        return self

    def save(self, path: str = "config.yaml") -> Path:
        p = Path(path)
        with open(p, "w", encoding="utf-8") as f:
            yaml.dump(self._data, f, default_flow_style=False,
                      allow_unicode=True, sort_keys=False)
        print(f"✅ 설정 저장: {p}")
        return p

    def show(self, section: Optional[str] = None) -> None:
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
        return copy.deepcopy(self._data)