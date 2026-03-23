"""2단계: 메타데이터 추출 + LLM 청킹 → chunks.json."""

import json
import re
import time
from pathlib import Path
from typing import Dict, List

from openai import OpenAI

from .config import Config
from .prompts import CHUNKING
from .utils import load_page_texts


def extract_metadata(book_dir: Path) -> List[dict]:
    """page_XXXX.md에서 페이지별 메타데이터 추출."""
    page_texts = load_page_texts(book_dir)
    meta = []
    for pn in sorted(page_texts.keys()):
        text = page_texts[pn]
        non_empty = [l.strip() for l in text.split("\n") if l.strip()]
        first_lines = [l[:30] if len(l) >= 40 else l for l in non_empty[:3]]
        meta.append({
            "page": pn,
            "char_count": len(text),
            "first_lines": first_lines,
        })
    return meta


def call_chunking_llm(page_meta: List[dict], model_config: dict) -> List[dict]:
    """LLM으로 자동 청킹 실행."""
    client = OpenAI()
    lines = []
    for m in page_meta:
        fl = " | ".join(m["first_lines"]) if m["first_lines"] else "(empty)"
        lines.append(f"Page {m['page']}: {m['char_count']}자, first=[{fl}]")

    user_prompt = (
        f"다음은 문서의 페이지별 정보입니다. 논리적 섹션으로 그룹화해주세요.\n\n"
        f"총 페이지 수: {len(page_meta)}\n\n" + "\n".join(lines)
    )

    kwargs = {
        **model_config,
        "messages": [
            {"role": "system", "content": CHUNKING},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    print(f"🔄 LLM 청킹 ({model_config['model']}, {len(page_meta)}p)...")
    response = client.chat.completions.create(**kwargs)
    raw = response.choices[0].message.content

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        fi, li = raw.find("{"), raw.rfind("}")
        result = json.loads(raw[fi : li + 1]) if fi != -1 and li > fi else None
    if not result or "chunks" not in result:
        raise ValueError(f"파싱 실패: {raw[:200]}")

    print(f"✅ 청킹 완료: {len(result['chunks'])}개")
    return result["chunks"]


def print_chunks(chunks: List[dict]) -> None:
    """청크 결과 출력."""
    print(f"\n📋 결과:")
    for c in chunks:
        pr = c["pages"]
        ps = f"p{pr[0]}-{pr[-1]}" if len(pr) > 1 else f"p{pr[0]}"
        print(f"  [{c['type']:7s}] {ps:10s} ({len(pr):2d}p) | {c['title']}")


def run_chunking(cfg: Config, book_dir: Path, book_name: str) -> List[dict]:
    """청킹 전체 실행 (진입점)."""
    page_meta = extract_metadata(book_dir)
    total = len(page_meta)
    print(f"\n📖 {book_name} ({total}p)")

    print(f"\n📋 메타데이터:")
    for m in page_meta[:10]:
        fl = " | ".join(m["first_lines"]) if m["first_lines"] else "(빈)"
        print(f"  p{m['page']:3d}: {m['char_count']:5d}자 | {fl[:70]}")
    if total > 10:
        print(f"  ... ({total - 10}개 더)")

    mode = cfg.get("chunking.mode", "auto")
    if mode == "manual":
        chunks = cfg.get("chunking.manual_chunks", [])
        print(f"\n📝 수동: {len(chunks)}개")
    else:
        model_config = cfg.get("chunking.model")
        chunks = call_chunking_llm(page_meta, model_config)

    print_chunks(chunks)

    # 저장
    chunks_data = {
        "book_name": book_name,
        "chunking_mode": mode,
        "model": cfg.get("chunking.model.model") if mode == "auto" else "manual",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_chunks": len(chunks),
        "chunks": chunks,
    }
    (book_dir / "chunks.json").write_text(
        json.dumps(chunks_data, ensure_ascii=False, indent=2), encoding="utf-8")

    meta_data = {
        "book_name": book_name,
        "total_pages": total,
        "total_chars": sum(m["char_count"] for m in page_meta),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pages": page_meta,
    }
    (book_dir / "metadata.json").write_text(
        json.dumps(meta_data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✅ 저장: chunks.json + metadata.json")
    return chunks
