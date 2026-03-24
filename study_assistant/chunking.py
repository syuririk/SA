"""2단계: 메타데이터 추출 + LLM 청킹 → chunks.json."""

import json
import re
import time
from pathlib import Path
from typing import List

from openai import OpenAI

from .config import Config
from .prompts import CHUNKING
from .utils import load_page_texts


def extract_metadata(book_dir):
    page_texts = load_page_texts(book_dir)
    meta = []
    for pn in sorted(page_texts.keys()):
        text = page_texts[pn]
        non_empty = [l.strip() for l in text.split("\n") if l.strip()]
        first_lines = [l[:30] if len(l) >= 40 else l for l in non_empty[:3]]
        meta.append({"page": pn, "char_count": len(text), "first_lines": first_lines})
    return meta


def call_chunking_llm(page_meta, model_config, user_instruction=""):
    client = OpenAI()
    lines = []
    for m in page_meta:
        fl = " | ".join(m["first_lines"]) if m["first_lines"] else "(empty)"
        lines.append(f"Page {m['page']}: {m['char_count']}자, first=[{fl}]")
    user_prompt = (f"다음은 문서의 페이지별 정보입니다. 논리적 섹션으로 그룹화해주세요.\n\n"
                   f"총 페이지 수: {len(page_meta)}\n\n" + "\n".join(lines))
    if user_instruction:
        user_prompt += f"\n\n추가 지시:\n{user_instruction}"
    kwargs = {**model_config,
              "messages": [{"role": "system", "content": CHUNKING},
                           {"role": "user", "content": user_prompt}],
              "response_format": {"type": "json_object"}}
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


def print_chunks(chunks):
    print(f"\n📋 청크 ({len(chunks)}개):")
    for i, c in enumerate(chunks):
        pr = c["pages"]
        ps = f"p{pr[0]}-{pr[-1]}" if len(pr) > 1 else f"p{pr[0]}"
        print(f"  [{i:2d}] [{c['type']:7s}] {ps:10s} ({len(pr):2d}p) | {c['title']}")


def validate_chunks(chunks, page_meta):
    issues = []
    all_pages = set(m["page"] for m in page_meta)
    covered = set()
    for i, c in enumerate(chunks):
        pp = c.get("pages", [])
        if not pp: issues.append(f"  [{i}]: 페이지 없음"); continue
        for j in range(len(pp)-1):
            if pp[j+1] != pp[j]+1: issues.append(f"  [{i}]: 불연속 {pp[j]}→{pp[j+1]}"); break
        ov = covered & set(pp)
        if ov: issues.append(f"  [{i}]: 중복 {sorted(ov)}")
        covered.update(pp)
    miss = all_pages - covered
    if miss: issues.append(f"  누락: {sorted(miss)}")
    return issues


def edit_chunks_interactive(chunks, page_meta):
    chunks = [dict(c) for c in chunks]
    while True:
        print_chunks(chunks)
        issues = validate_chunks(chunks, page_meta)
        if issues:
            print("\n⚠️ 검증:")
            for iss in issues: print(iss)
        cmd = input("\n명령 (done/retry/edit N/split N P/merge N M/type N T/title N T/delete N): ").strip()
        if not cmd: continue
        parts = cmd.split(maxsplit=2)
        act = parts[0].lower()
        if act == "done":
            fi = validate_chunks(chunks, page_meta)
            if fi:
                for iss in fi: print(iss)
                if input("저장? (y/n): ").strip().lower() != 'y': continue
            return chunks
        elif act == "retry": return None
        elif act in ("show","validate"): continue
        elif act == "edit" and len(parts) >= 2:
            try:
                i = int(parts[1]); c = chunks[i]
                t = input(f"  제목 ({c['title']}): ").strip()
                tp = input(f"  타입 ({c['type']}): ").strip()
                pg = input(f"  페이지 ({c['pages']}): ").strip()
                if t: c["title"] = t
                if tp in ("toc","heading","content","quiz"): c["type"] = tp
                if pg: c["pages"] = json.loads(pg)
            except Exception as e: print(f"  ❌ {e}")
        elif act == "split" and len(parts) >= 3:
            try:
                i, sp = int(parts[1]), int(parts[2]); c = chunks[i]; pp = c["pages"]
                si = pp.index(sp)
                chunks[i:i+1] = [{"title":c["title"]+" (상)","pages":pp[:si],"type":c["type"]},
                                 {"title":c["title"]+" (하)","pages":pp[si:],"type":c["type"]}]
            except Exception as e: print(f"  ❌ {e}")
        elif act == "merge" and len(parts) >= 3:
            try:
                a, b = int(parts[1]), int(parts[2]); lo, hi = min(a,b), max(a,b)
                chunks[lo:hi+1] = [{"title":chunks[lo]["title"],"pages":chunks[lo]["pages"]+chunks[hi]["pages"],"type":chunks[lo]["type"]}]
            except Exception as e: print(f"  ❌ {e}")
        elif act == "type" and len(parts) >= 3:
            try: chunks[int(parts[1])]["type"] = parts[2].lower()
            except Exception as e: print(f"  ❌ {e}")
        elif act == "title" and len(parts) >= 3:
            try: chunks[int(parts[1])]["title"] = parts[2]
            except Exception as e: print(f"  ❌ {e}")
        elif act == "delete" and len(parts) >= 2:
            try:
                r = chunks.pop(int(parts[1]))
                print(f"  ✅ 삭제: {r['title']} (⚠️ p{r['pages']} 재배치)")
            except Exception as e: print(f"  ❌ {e}")


def save_chunks(chunks, book_dir, book_name, page_meta, mode, model_name):
    book_dir = Path(book_dir)
    (book_dir / "chunks.json").write_text(json.dumps({
        "book_name": book_name, "chunking_mode": mode, "model": model_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_chunks": len(chunks), "chunks": chunks,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (book_dir / "metadata.json").write_text(json.dumps({
        "book_name": book_name, "total_pages": len(page_meta),
        "total_chars": sum(m["char_count"] for m in page_meta),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pages": page_meta,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 저장: {book_dir}/chunks.json")


def run_chunking(cfg, book_name):
    """OCR 디렉토리에서 읽고 chunks.json 저장."""
    book_dir = cfg.book_ocr_dir(book_name)
    if not book_dir.exists():
        raise FileNotFoundError(f"OCR 없음: {book_dir}")
    page_meta = extract_metadata(book_dir)
    print(f"\n📖 {book_name} ({len(page_meta)}p)")
    print(f"   OCR: {book_dir}")

    cache = book_dir / "chunks.json"
    if cache.exists():
        with open(cache, "r", encoding="utf-8") as f: cached = json.load(f)
        print(f"\n⚠️ 기존 chunks.json ({cached.get('model','?')}, {cached.get('created_at','?')})")
        print_chunks(cached.get("chunks", []))
        ch = input("\n  [r] 편집  [n] 새로 요청  [s] 그대로: ").strip().lower()
        if ch == "s": return cached["chunks"]
        elif ch == "r":
            result = edit_chunks_interactive(cached["chunks"], page_meta)
            if result is not None:
                save_chunks(result, book_dir, book_name, page_meta,
                            cached.get("chunking_mode","auto"), cached.get("model","?"))
                return result

    for m in page_meta[:10]:
        fl = " | ".join(m["first_lines"]) if m["first_lines"] else "(빈)"
        print(f"  p{m['page']:3d}: {m['char_count']:5d}자 | {fl[:70]}")
    if len(page_meta) > 10: print(f"  ... ({len(page_meta)-10}개 더)")

    mode = cfg.get("chunking.mode", "auto")
    mc = cfg.get("chunking.model")
    mn = mc.get("model","?") if isinstance(mc, dict) else "manual"
    chunks = cfg.get("chunking.manual_chunks",[]) if mode == "manual" else call_chunking_llm(page_meta, mc)

    while True:
        result = edit_chunks_interactive(chunks, page_meta)
        if result is None:
            instr = input("\n📝 추가 지시: ").strip()
            chunks = call_chunking_llm(page_meta, mc, instr)
        else:
            chunks = result; break

    save_chunks(chunks, book_dir, book_name, page_meta, mode, mn)
    return chunks
