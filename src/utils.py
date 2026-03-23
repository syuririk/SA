"""공용 유틸리티 함수."""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def safe_filename(name: str) -> str:
    """파일명에 사용할 수 없는 문자를 _ 로 치환."""
    return re.sub(r'[<>:"/\\|?*\s]+', '_', name)


def extract_wikilinks(text: str) -> List[str]:
    """텍스트에서 [[wikilink]] 추출."""
    return sorted(set(re.findall(r"\[\[([^\]]+)\]\]", text)))


def expand_page_range(pages: List[int]) -> List[int]:
    """[4, 10] 같은 불완전 범위를 [4,5,6,7,8,9,10]으로 확장."""
    if not pages:
        return []
    pages = [int(p) for p in pages]
    if len(pages) <= 1:
        return pages
    if len(pages) == 2 and pages[1] > pages[0] + 1:
        return list(range(pages[0], pages[1] + 1))
    is_consecutive = all(pages[i] + 1 == pages[i + 1] for i in range(len(pages) - 1))
    if not is_consecutive and pages[-1] > pages[0] + len(pages):
        return list(range(pages[0], pages[-1] + 1))
    return pages


def enforce_prefix(data: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """파일명에 prefix를 강제 적용."""
    fixed = {}
    for filename, info in data.items():
        name = filename
        if not name.startswith(prefix):
            for old in ("Summary_", "Quiz_", "CQuiz_"):
                if name.startswith(old):
                    name = name[len(old):]
                    break
            name = prefix + name
        if not name.endswith(".md"):
            name += ".md"
        fixed[name] = info
    return fixed


def postprocess_metadata(
    data: Dict[str, Any], file_type: str, chunk_id: str
) -> Dict[str, Any]:
    """LLM 응답 파일에 메타데이터를 부착."""
    result = {}
    for filename, info in data.items():
        content = info.get("content", "")
        result[filename] = {
            "content": content,
            "metadata": {
                "file_type": file_type,
                "source_chunk": f"[[{chunk_id}]]",
                "key_concepts": info.get("key_concepts", []),
                "concepts": [f"[[{c}]]" for c in extract_wikilinks(content)],
            },
        }
    return result


class JSONParser:
    """LLM JSON 응답을 여러 전략으로 파싱."""

    @staticmethod
    def parse(raw: str) -> Optional[dict]:
        for fn in [JSONParser._direct, JSONParser._fence, JSONParser._brace]:
            result = fn(raw)
            if result is not None:
                return result
        return None

    @staticmethod
    def _direct(raw: str) -> Optional[dict]:
        try:
            return json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _fence(raw: str) -> Optional[dict]:
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        try:
            return json.loads(cleaned.strip())
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _brace(raw: str) -> Optional[dict]:
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last > first:
            try:
                return json.loads(raw[first : last + 1])
            except (json.JSONDecodeError, ValueError):
                return None
        return None


def list_books(drive_base: str) -> List[dict]:
    """OCR 완료된 교재 목록 반환."""
    base = Path(drive_base)
    books = []
    if not base.exists():
        return books
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        md_count = len(list(d.glob("page_*.md")))
        if md_count > 0:
            has_chunks = (d / "chunks.json").exists()
            chunk_types = ""
            if has_chunks:
                with open(d / "chunks.json", "r", encoding="utf-8") as f:
                    cd = json.load(f)
                types = {}
                for c in cd.get("chunks", []):
                    t = c.get("type", "?")
                    types[t] = types.get(t, 0) + 1
                chunk_types = ", ".join(f"{t}:{n}" for t, n in types.items())
            books.append({
                "name": d.name,
                "path": d,
                "pages": md_count,
                "has_chunks": has_chunks,
                "chunk_types": chunk_types,
            })
    return books


def print_books(books: List[dict]) -> None:
    """교재 목록 출력."""
    if not books:
        print("⚠️ 교재 없음")
        return
    print("📚 교재 목록:")
    for i, b in enumerate(books):
        mark = ""
        if b["has_chunks"]:
            mark = f" ✅chunks ({b['chunk_types']})"
        print(f"  [{i}] {b['name']} — {b['pages']}p{mark}")


def load_page_texts(book_dir: Path) -> Dict[int, str]:
    """교재 디렉토리에서 page_XXXX.md를 읽어 {페이지번호: 본문} 반환."""
    page_texts = {}
    for md_path in sorted(book_dir.glob("page_*.md")):
        raw = md_path.read_text(encoding="utf-8")
        body = re.sub(r'^---\n.*?\n---\n', '', raw, flags=re.DOTALL).strip()
        body = re.sub(r'^# Page \d+\s*\n', '', body).strip()
        match = re.search(r'page_(\d+)', md_path.stem)
        page_num = int(match.group(1)) if match else 0
        page_texts[page_num] = body
    return page_texts
