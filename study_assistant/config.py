"""설정 관리 클래스."""

import copy
import os
import yaml
from pathlib import Path
from typing import Any, Optional


_DEFAULT_CONFIG = {
    "paths": {
        "root": "/content/drive/MyDrive/Syuririk/100 For Agent",
        "pdf": "{root}/PDFs",
        "ocr": "{root}/OCR",
        "vault": "{root}/Vault",
    },
    "book": {"index": 0},
    "ocr": {
        "model": "mistral-ocr-latest",
        "batch_size": 50,
        "max_concurrent": 3,
    },
    "chunking": {
        "mode": "auto",
        "model": {"model": "gpt-4.1-mini", "temperature": 0.2},
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
    "graph_rag": {
        "model": {"model": "gpt-5-mini"},
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
    """설정 관리.

        cfg = Config()
        cfg = Config("config.yaml")
        cfg.get("paths.vault")       # 자동으로 {root} 치환된 경로 반환
        cfg.set("book.index", 2)
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

    def _resolve(self, value: Any) -> Any:
        """paths 값의 {root} 치환."""
        if isinstance(value, str) and "{root}" in value:
            return value.replace("{root}", self._data.get("paths", {}).get("root", ""))
        return value

    def get(self, key: str, default: Any = None) -> Any:
        parts = key.split(".")
        node = self._data
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return self._resolve(node)

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
        if section == "paths" or (section is None and isinstance(data, dict) and "paths" in data):
            self._print_dict(data, resolve_paths=True)
        else:
            self._print_dict(data)
        print(f"{'━' * 50}\n")

    def _print_dict(self, data: Any, indent: int = 0, resolve_paths: bool = False) -> None:
        prefix = "  " * indent
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    print(f"{prefix}{k}:")
                    self._print_dict(v, indent + 1, resolve_paths)
                elif isinstance(v, list) and len(v) > 3:
                    print(f"{prefix}{k}: [{len(v)} items]")
                elif resolve_paths:
                    print(f"{prefix}{k}: {self._resolve(v)}")
                else:
                    print(f"{prefix}{k}: {v}")
        else:
            print(f"{prefix}{data}")

    # ── 경로 프로퍼티 ─────────────────────────────

    @property
    def root(self) -> str:
        return self.get("paths.root")

    @property
    def pdf_dir(self) -> str:
        return self.get("paths.pdf")

    @property
    def ocr_dir(self) -> str:
        return self.get("paths.ocr")

    @property
    def vault_dir(self) -> str:
        return self.get("paths.vault")

    @property
    def book_index(self) -> int:
        return self.get("book.index", 0)

    def book_ocr_dir(self, book_name: str) -> Path:
        """특정 교재의 OCR 디렉토리."""
        return Path(self.ocr_dir) / book_name

    def book_vault_dir(self, book_name: str) -> Path:
        """특정 교재의 vault 디렉토리."""
        return Path(self.vault_dir) / book_name

    def ensure_dirs(self) -> None:
        """모든 기본 경로 생성."""
        for d in [self.root, self.pdf_dir, self.ocr_dir, self.vault_dir]:
            os.makedirs(d, exist_ok=True)

    @property
    def raw(self) -> dict:
        return copy.deepcopy(self._data)