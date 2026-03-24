"""공용 유틸리티 함수."""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]+', '_', name)


def extract_wikilinks(text: str) -> List[str]:
    return sorted(set(re.findall(r"\[\[([^\]]+)\]\]", text)))


def expand_page_range(pages: List[int]) -> List[int]:
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


def postprocess_metadata(data: Dict[str, Any], file_type: str, chunk_id: str) -> Dict[str, Any]:
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
    @staticmethod
    def parse(raw: str) -> Optional[dict]:
        for fn in [JSONParser._direct, JSONParser._fence, JSONParser._brace]:
            result = fn(raw)
            if result is not None:
                return result
        return None

    @staticmethod
    def _direct(raw):
        try: return json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError): return None

    @staticmethod
    def _fence(raw):
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        try: return json.loads(cleaned.strip())
        except (json.JSONDecodeError, ValueError): return None

    @staticmethod
    def _brace(raw):
        first, last = raw.find("{"), raw.rfind("}")
        if first != -1 and last > first:
            try: return json.loads(raw[first:last+1])
            except (json.JSONDecodeError, ValueError): return None
        return None


def load_page_texts(book_dir: Path) -> Dict[int, str]:
    page_texts = {}
    book_dir = Path(book_dir)
    for md_path in sorted(book_dir.glob("page_*.md")):
        raw = md_path.read_text(encoding="utf-8")
        body = re.sub(r'^---\n.*?\n---\n', '', raw, flags=re.DOTALL).strip()
        body = re.sub(r'^# Page \d+\s*\n', '', body).strip()
        match = re.search(r'page_(\d+)', md_path.stem)
        page_num = int(match.group(1)) if match else 0
        page_texts[page_num] = body
    return page_texts


# ══════════════════════════════════════════════════
# 목록 조회 함수
# ══════════════════════════════════════════════════

def list_pdfs(pdf_dir: str) -> List[dict]:
    """PDF 폴더의 파일 목록."""
    base = Path(pdf_dir)
    pdfs = []
    if not base.exists():
        return pdfs
    for p in sorted(base.glob("*.pdf")):
        size_mb = p.stat().st_size / (1024 * 1024)
        pdfs.append({"name": p.stem, "path": p, "size_mb": round(size_mb, 1)})
    return pdfs


def list_ocr(ocr_dir: str) -> List[dict]:
    """OCR 완료된 교재 목록 (page_*.md 존재 여부)."""
    base = Path(ocr_dir)
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
                "name": d.name, "path": d, "pages": md_count,
                "has_chunks": has_chunks, "chunk_types": chunk_types,
            })
    return books


def list_vaults(vault_dir: str) -> List[dict]:
    """Vault 완료된 교재 목록."""
    base = Path(vault_dir)
    vaults = []
    if not base.exists():
        return vaults
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        has_toc = (d / "Master_ToC.md").exists()
        has_index = (d / "vault_index.json").exists()
        summaries = len(list((d / "Summaries").glob("*.md"))) if (d / "Summaries").exists() else 0
        quizzes = len(list((d / "Quizzes").glob("*.md"))) if (d / "Quizzes").exists() else 0
        gen_quizzes = len(list((d / "Generated_Quizzes").glob("*.md"))) if (d / "Generated_Quizzes").exists() else 0
        if has_toc or has_index or summaries > 0:
            vaults.append({
                "name": d.name, "path": d,
                "summaries": summaries, "quizzes": quizzes,
                "generated_quizzes": gen_quizzes,
                "has_index": has_index,
            })
    return vaults


def print_pdfs(pdfs: List[dict]) -> None:
    if not pdfs:
        print("📄 PDF 없음")
        return
    print("📄 PDF 목록:")
    for i, p in enumerate(pdfs):
        print(f"  [{i}] {p['name']}.pdf ({p['size_mb']}MB)")


def print_ocr(books: List[dict]) -> None:
    if not books:
        print("📝 OCR 데이터 없음")
        return
    print("📝 OCR 목록:")
    for i, b in enumerate(books):
        mark = f" ✅chunks ({b['chunk_types']})" if b["has_chunks"] else ""
        print(f"  [{i}] {b['name']} — {b['pages']}p{mark}")


def print_vaults(vaults: List[dict]) -> None:
    if not vaults:
        print("📚 Vault 없음")
        return
    print("📚 Vault 목록:")
    for i, v in enumerate(vaults):
        parts = []
        if v["summaries"]: parts.append(f"요약:{v['summaries']}")
        if v["quizzes"]: parts.append(f"퀴즈:{v['quizzes']}")
        if v["generated_quizzes"]: parts.append(f"생성퀴즈:{v['generated_quizzes']}")
        detail = ", ".join(parts) if parts else "비어있음"
        print(f"  [{i}] {v['name']} — {detail}")


def print_all(cfg) -> None:
    """모든 데이터 현황을 한번에 출력."""
    print(f"\n{'═' * 50}")
    print(f"📂 루트: {cfg.root}")
    print(f"{'═' * 50}")
    print_pdfs(list_pdfs(cfg.pdf_dir))
    print()
    print_ocr(list_ocr(cfg.ocr_dir))
    print()
    print_vaults(list_vaults(cfg.vault_dir))
    print(f"{'═' * 50}\n")
