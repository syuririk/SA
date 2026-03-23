"""2단계: 메타데이터 추출 + LLM 청킹 → chunks.json."""

import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from openai import OpenAI

from .config import Config
from .prompts import CHUNKING
from .utils import load_page_texts


def extract_metadata(book_dir: Path) -> List[dict]:
    page_texts = load_page_texts(book_dir)
    meta = []
    for pn in sorted(page_texts.keys()):
        text = page_texts[pn]
        non_empty = [l.strip() for l in text.split("\n") if l.strip()]
        first_lines = [l[:30] if len(l) >= 40 else l for l in non_empty[:3]]
        meta.append({"page": pn, "char_count": len(text), "first_lines": first_lines})
    return meta


def call_chunking_llm(
    page_meta: List[dict],
    model_config: dict,
    user_instruction: str = "",
) -> List[dict]:
    """LLM으로 자동 청킹. user_instruction이 있으면 프롬프트에 추가."""
    client = OpenAI()
    lines = []
    for m in page_meta:
        fl = " | ".join(m["first_lines"]) if m["first_lines"] else "(empty)"
        lines.append(f"Page {m['page']}: {m['char_count']}자, first=[{fl}]")

    user_prompt = (
        f"다음은 문서의 페이지별 정보입니다. 논리적 섹션으로 그룹화해주세요.\n\n"
        f"총 페이지 수: {len(page_meta)}\n\n" + "\n".join(lines)
    )

    if user_instruction:
        user_prompt += f"\n\n추가 지시:\n{user_instruction}"

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
        result = json.loads(raw[fi:li+1]) if fi != -1 and li > fi else None
    if not result or "chunks" not in result:
        raise ValueError(f"파싱 실패: {raw[:200]}")
    print(f"✅ 청킹 완료: {len(result['chunks'])}개")
    return result["chunks"]


def print_chunks(chunks: List[dict]) -> None:
    """청크 목록을 번호와 함께 출력."""
    print(f"\n📋 청크 목록 ({len(chunks)}개):")
    all_pages = set()
    for i, c in enumerate(chunks):
        pr = c["pages"]
        ps = f"p{pr[0]}-{pr[-1]}" if len(pr) > 1 else f"p{pr[0]}"
        print(f"  [{i:2d}] [{c['type']:7s}] {ps:10s} ({len(pr):2d}p) | {c['title']}")
        all_pages.update(pr)
    print(f"\n  총 페이지: {len(all_pages)}p")


def validate_chunks(chunks: List[dict], page_meta: List[dict]) -> List[str]:
    """청크 유효성 검증. 문제가 있으면 메시지 리스트 반환."""
    issues = []
    all_pages = set(m["page"] for m in page_meta)
    covered = set()
    for i, c in enumerate(chunks):
        pages = c.get("pages", [])
        if not pages:
            issues.append(f"  청크 [{i}]: 페이지 비어있음")
            continue
        # 연속성 체크
        for j in range(len(pages) - 1):
            if pages[j + 1] != pages[j] + 1:
                issues.append(f"  청크 [{i}]: 페이지 불연속 ({pages[j]} → {pages[j+1]})")
                break
        # 중복 체크
        overlap = covered & set(pages)
        if overlap:
            issues.append(f"  청크 [{i}]: 중복 페이지 {sorted(overlap)}")
        covered.update(pages)
    # 누락 체크
    missing = all_pages - covered
    if missing:
        issues.append(f"  누락 페이지: {sorted(missing)}")
    return issues


def edit_chunks_interactive(chunks: List[dict], page_meta: List[dict]) -> List[dict]:
    """인터랙티브 청크 편집 루프.

    명령어:
      done          → 편집 종료, 저장
      retry         → LLM에 추가 지시를 넣어 재청킹 (미구현, 외부에서 처리)
      edit N        → N번 청크 수정
      split N P     → N번 청크를 P페이지 기준으로 분할
      merge N M     → N번과 M번 청크 병합
      type N TYPE   → N번 청크 타입 변경 (toc/heading/content/quiz)
      title N TEXT  → N번 청크 제목 변경
      delete N      → N번 청크 삭제
      show          → 현재 청크 목록 출력
      validate      → 유효성 검증
    """
    chunks = [dict(c) for c in chunks]  # 깊은 복사

    while True:
        print_chunks(chunks)
        issues = validate_chunks(chunks, page_meta)
        if issues:
            print("\n⚠️ 검증 문제:")
            for iss in issues:
                print(iss)

        cmd = input("\n명령어 (done/retry/edit/split/merge/type/title/delete/show/validate): ").strip()

        if not cmd:
            continue

        parts = cmd.split(maxsplit=2)
        action = parts[0].lower()

        if action == "done":
            final_issues = validate_chunks(chunks, page_meta)
            if final_issues:
                print("\n⚠️ 아직 문제가 있습니다:")
                for iss in final_issues:
                    print(iss)
                if input("그래도 저장? (y/n): ").strip().lower() != 'y':
                    continue
            print("✅ 편집 완료")
            return chunks

        elif action == "retry":
            return None  # 호출자에서 재청킹 처리

        elif action == "show":
            continue  # 루프 시작 시 출력됨

        elif action == "validate":
            if not issues:
                print("✅ 문제 없음")
            continue

        elif action == "edit" and len(parts) >= 2:
            try:
                idx = int(parts[1])
                c = chunks[idx]
                print(f"\n  현재: [{c['type']}] p{c['pages'][0]}-{c['pages'][-1]} | {c['title']}")
                new_title = input(f"  제목 ({c['title']}): ").strip()
                new_type = input(f"  타입 ({c['type']}) [toc/heading/content/quiz]: ").strip()
                new_pages = input(f"  페이지 ({c['pages']}): ").strip()
                if new_title:
                    c["title"] = new_title
                if new_type in ("toc", "heading", "content", "quiz"):
                    c["type"] = new_type
                if new_pages:
                    c["pages"] = json.loads(new_pages)
                print(f"  ✅ 수정됨")
            except (ValueError, IndexError) as e:
                print(f"  ❌ 오류: {e}")

        elif action == "split" and len(parts) >= 3:
            try:
                idx = int(parts[1])
                split_page = int(parts[2])
                c = chunks[idx]
                pages = c["pages"]
                if split_page not in pages or split_page == pages[0]:
                    print(f"  ❌ p{split_page}는 청크 [{idx}]의 분할 가능한 위치가 아닙니다")
                    continue
                split_idx = pages.index(split_page)
                chunk_a = {"title": c["title"] + " (상)", "pages": pages[:split_idx], "type": c["type"]}
                chunk_b = {"title": c["title"] + " (하)", "pages": pages[split_idx:], "type": c["type"]}
                chunks[idx:idx+1] = [chunk_a, chunk_b]
                print(f"  ✅ 청크 [{idx}]를 p{split_page} 기준으로 분할")
            except (ValueError, IndexError) as e:
                print(f"  ❌ 오류: {e}")

        elif action == "merge" and len(parts) >= 3:
            try:
                a, b = int(parts[1]), int(parts[2])
                if abs(a - b) != 1:
                    print(f"  ❌ 인접한 청크만 병합 가능")
                    continue
                lo, hi = min(a, b), max(a, b)
                merged = {
                    "title": chunks[lo]["title"],
                    "pages": chunks[lo]["pages"] + chunks[hi]["pages"],
                    "type": chunks[lo]["type"],
                }
                chunks[lo:hi+1] = [merged]
                print(f"  ✅ 청크 [{lo}]과 [{hi}] 병합")
            except (ValueError, IndexError) as e:
                print(f"  ❌ 오류: {e}")

        elif action == "type" and len(parts) >= 3:
            try:
                idx = int(parts[1])
                new_type = parts[2].lower()
                if new_type not in ("toc", "heading", "content", "quiz"):
                    print(f"  ❌ 유효한 타입: toc, heading, content, quiz")
                    continue
                chunks[idx]["type"] = new_type
                print(f"  ✅ 청크 [{idx}] 타입 → {new_type}")
            except (ValueError, IndexError) as e:
                print(f"  ❌ 오류: {e}")

        elif action == "title" and len(parts) >= 3:
            try:
                idx = int(parts[1])
                new_title = parts[2]
                chunks[idx]["title"] = new_title
                print(f"  ✅ 청크 [{idx}] 제목 → {new_title}")
            except (ValueError, IndexError) as e:
                print(f"  ❌ 오류: {e}")

        elif action == "delete" and len(parts) >= 2:
            try:
                idx = int(parts[1])
                removed = chunks.pop(idx)
                print(f"  ✅ 청크 [{idx}] 삭제: {removed['title']}")
                print(f"  ⚠️ 삭제된 페이지 {removed['pages']}를 다른 청크에 추가하세요")
            except (ValueError, IndexError) as e:
                print(f"  ❌ 오류: {e}")

        else:
            print("  ❓ 명령어: done / retry / edit N / split N P / merge N M / type N TYPE / title N TEXT / delete N / show / validate")


def save_chunks(chunks: List[dict], book_dir: Path, book_name: str,
                page_meta: List[dict], mode: str, model_name: str) -> None:
    """청크와 메타데이터를 저장."""
    book_dir = Path(book_dir)
    chunks_data = {
        "book_name": book_name,
        "chunking_mode": mode,
        "model": model_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_chunks": len(chunks),
        "chunks": chunks,
    }
    (book_dir / "chunks.json").write_text(
        json.dumps(chunks_data, ensure_ascii=False, indent=2), encoding="utf-8")

    meta_data = {
        "book_name": book_name, "total_pages": len(page_meta),
        "total_chars": sum(m["char_count"] for m in page_meta),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pages": page_meta,
    }
    (book_dir / "metadata.json").write_text(
        json.dumps(meta_data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✅ 저장: chunks.json + metadata.json")


def run_chunking(cfg, book_dir, book_name):
    """청킹 전체 실행 (진입점). 인터랙티브 편집 포함."""
    book_dir = Path(book_dir)
    page_meta = extract_metadata(book_dir)
    total = len(page_meta)
    print(f"\n📖 {book_name} ({total}p)")

    # 캐시 확인
    cache_path = book_dir / "chunks.json"
    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        cached_chunks = cached.get("chunks", [])
        cached_model = cached.get("model", "?")
        cached_time = cached.get("created_at", "?")
        print(f"\n⚠️ 기존 chunks.json 발견")
        print(f"   모델: {cached_model} | 생성: {cached_time} | {len(cached_chunks)}개 청크")
        print_chunks(cached_chunks)

        choice = input("\n  [r] 기존 결과로 편집  [n] 새로 LLM 요청  [s] 그대로 사용: ").strip().lower()
        if choice == "s":
            print("✅ 기존 청크 사용")
            return cached_chunks
        elif choice == "r":
            print("📝 기존 결과 편집 모드")
            result = edit_chunks_interactive(cached_chunks, page_meta)
            if result is not None:
                mode = cached.get("chunking_mode", "auto")
                save_chunks(result, book_dir, book_name, page_meta, mode, cached_model)
                return result
            # retry 선택 시 아래로 계속 진행

    print(f"\n📋 메타데이터:")
    for m in page_meta[:10]:
        fl = " | ".join(m["first_lines"]) if m["first_lines"] else "(빈)"
        print(f"  p{m['page']:3d}: {m['char_count']:5d}자 | {fl[:70]}")
    if total > 10:
        print(f"  ... ({total - 10}개 더)")

    mode = cfg.get("chunking.mode", "auto")
    model_config = cfg.get("chunking.model")
    model_name = model_config.get("model", "unknown") if isinstance(model_config, dict) else "manual"
    user_instruction = ""

    if mode == "manual":
        chunks = cfg.get("chunking.manual_chunks", [])
        print(f"\n📝 수동: {len(chunks)}개")
        model_name = "manual"
    else:
        chunks = call_chunking_llm(page_meta, model_config)

    # 인터랙티브 편집 루프
    while True:
        result = edit_chunks_interactive(chunks, page_meta)
        if result is None:
            # retry 요청 → 추가 지시 받아서 재청킹
            user_instruction = input("\n📝 LLM에 추가 지시 (예: 'p5-10을 하나로 합쳐줘'): ").strip()
            if user_instruction:
                chunks = call_chunking_llm(page_meta, model_config, user_instruction)
            else:
                chunks = call_chunking_llm(page_meta, model_config)
        else:
            chunks = result
            break

    save_chunks(chunks, book_dir, book_name, page_meta, mode, model_name)
    return chunks