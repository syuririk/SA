"""3단계: 요약 + 퀴즈 → Obsidian vault."""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from .config import Config
from .prompts import SUMMARY, QUIZ_EXTRACT, QUIZ_CREATE
from .utils import (
    enforce_prefix, expand_page_range, extract_wikilinks,
    load_page_texts, postprocess_metadata, JSONParser,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
log = logging.getLogger("Pipeline")

TYPE_TO_FOLDER = {"ToC": "", "Source": "Sources", "Summary": "Summaries",
                  "Quiz": "Quizzes", "Created_Quiz": "Quizzes"}
CHUNK_ICONS = {"toc": "📑 목차/서문", "heading": "📌 표제",
               "content": "📖 학습 내용", "quiz": "❓ 연습문제"}


async def call_llm(client, semaphore, system_prompt, user_prompt, model_config,
                   max_retries=3, retry_delay=5):
    async with semaphore:
        for attempt in range(1, max_retries + 1):
            try:
                extra = "\n\nReturn ONLY raw JSON." if attempt > 1 else ""
                kwargs = {**model_config,
                          "messages": [{"role": "system", "content": system_prompt},
                                       {"role": "user", "content": user_prompt + extra}],
                          "response_format": {"type": "json_object"}}
                response = await client.chat.completions.create(**kwargs)
                parsed = JSONParser.parse(response.choices[0].message.content)
                if parsed is not None:
                    return parsed
                log.warning(f"시도 {attempt}/{max_retries}: 파싱 실패")
            except Exception as e:
                log.error(f"시도 {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                await asyncio.sleep(retry_delay * attempt)
    return None


async def summarize_and_detect(chunk, client, sem, cfg):
    prompt = (f"Source: {chunk['id']} — {chunk['title']}\n"
              f"Pages: {chunk.get('pages', [])}\n\nStudy material:\n{chunk['text']}")
    raw = await call_llm(client, sem, SUMMARY, prompt, cfg.get("pipeline.summary"),
                         cfg.get("pipeline.max_retries", 3), cfg.get("pipeline.retry_delay", 5))
    if raw is None: return {}, False, False
    has_q, needs_q = raw.get("has_quiz", False), raw.get("needs_quiz", True)
    files = raw.get("files", {})
    if not files: files = {k: v for k, v in raw.items() if k.endswith(".md")}
    files = enforce_prefix(files, "Summary_")
    return postprocess_metadata(files, "Summary", chunk["id"]), has_q, needs_q


async def extract_quizzes(chunk, client, sem, cfg):
    prompt = (f"Source: {chunk['id']} — {chunk['title']}\n\nStudy material:\n{chunk['text']}")
    raw = await call_llm(client, sem, QUIZ_EXTRACT, prompt, cfg.get("pipeline.quiz_extract"),
                         cfg.get("pipeline.max_retries", 3), cfg.get("pipeline.retry_delay", 5))
    if not raw: return None
    raw = enforce_prefix(raw, "Quiz_")
    return postprocess_metadata(raw, "Quiz", chunk["id"])


async def create_quizzes(chunk, summary_data, existing_quiz_data, client, sem, cfg):
    all_kc = set()
    for info in summary_data.values():
        for kc in info.get("metadata", {}).get("key_concepts", []): all_kc.add(kc.strip("[]"))
    if not all_kc: return None
    covered = set()
    for info in existing_quiz_data.values():
        for lk in extract_wikilinks(info.get("content", "")): covered.add(lk)
    uncov = all_kc - covered
    if not uncov: return None
    log.info(f"    미커버 {len(uncov)}개")
    us = ", ".join([f"[[{c}]]" for c in uncov])
    prompt = (f"Create review questions for: {us}\n\n"
              f"Source: {chunk['id']} — {chunk['title']}\n\nContext:\n{chunk['text']}")
    raw = await call_llm(client, sem, QUIZ_CREATE, prompt, cfg.get("pipeline.quiz_create"),
                         cfg.get("pipeline.max_retries", 3), cfg.get("pipeline.retry_delay", 5))
    if not raw: return None
    raw = enforce_prefix(raw, "CQuiz_")
    return postprocess_metadata(raw, "Created_Quiz", chunk["id"])


async def process_content_chunk(chunk, client, sem, cfg):
    results = {}; cid = chunk["id"]
    log.info(f"  📖 {cid}: ({len(chunk['pages'])}p, {len(chunk['text']):,}자)")
    summary, has_q, needs_q = await summarize_and_detect(chunk, client, sem, cfg)
    if summary: results.update(summary); log.info(f"  {cid}: Summary {len(summary)}개")
    if not needs_q: log.info(f"  {cid}: 퀴즈 불필요 → 스킵"); return results
    quizzes = {}
    if has_q:
        quizzes = await extract_quizzes(chunk, client, sem, cfg) or {}
        if quizzes: results.update(quizzes); log.info(f"  {cid}: Quiz_ {len(quizzes)}개")
    new_q = await create_quizzes(chunk, summary, quizzes, client, sem, cfg) or {}
    if new_q: results.update(new_q); log.info(f"  {cid}: CQuiz_ {len(new_q)}개")
    return results


async def process_quiz_chunk(chunk, client, sem, cfg):
    cid = chunk["id"]; log.info(f"  ❓ {cid}: 퀴즈 추출")
    q = await extract_quizzes(chunk, client, sem, cfg) or {}
    if q: log.info(f"  {cid}: Quiz_ {len(q)}개")
    return q


def build_chunk_document(chunk):
    cid, title, text = chunk["id"], chunk["title"], chunk["text"]
    label = CHUNK_ICONS.get(chunk.get("type", "content"), "📄")
    pp = chunk.get("pages", [])
    pi = f"p.{pp[0]}-{pp[-1]}" if len(pp) > 1 else f"p.{pp[0]}" if pp else ""
    content = f"# {title}\n\n> {label} | {pi} | {len(pp)}페이지\n\n{text}"
    return {f"{cid}.md": {"content": content, "metadata": {
        "file_type": "Source", "source_chunk": f"[[{cid}]]",
        "key_concepts": [], "concepts": [f"[[{c}]]" for c in extract_wikilinks(text)]}}}


def generate_toc(chunks, all_files):
    lines = ["# 📚 Master Table of Contents\n", "> 중앙 허브\n"]
    cf = defaultdict(lambda: {"source":[],"summary":[],"quiz":[],"created_quiz":[]})
    for fn, info in all_files.items():
        m = info.get("metadata", {}); ft = m.get("file_type",""); src = m.get("source_chunk","").strip("[]")
        nne = fn.replace(".md","")
        if ft=="Source": cf[src]["source"].append(nne)
        elif ft=="Summary": cf[src]["summary"].append(nne)
        elif ft=="Quiz": cf[src]["quiz"].append(nne)
        elif ft=="Created_Quiz": cf[src]["created_quiz"].append(nne)
    for chunk in chunks:
        cid,title = chunk["id"],chunk["title"]; files = cf.get(cid,{})
        icon = {"toc":"📑","heading":"📌","content":"📖","quiz":"❓"}.get(chunk.get("type"),"📄")
        pr = chunk.get("pages",[]); ps = f"p.{pr[0]}-{pr[-1]}" if len(pr)>1 else f"p.{pr[0]}" if pr else ""
        lines.append(f"\n## {icon} [[Sources/{cid}|{title}]] ({ps})\n")
        for s in files.get("summary",[]): lines.append(f"- [[Summaries/{s}]]")
        qs = files.get("quiz",[]) + files.get("created_quiz",[])
        if qs:
            lines.append("### 퀴즈")
            for q in qs: lines.append(f"- [[Quizzes/{q}]]")
    content = "\n".join(lines)
    return {"Master_ToC.md":{"content":content,"metadata":{
        "file_type":"ToC","source_chunk":"[[root]]",
        "key_concepts":[],"concepts":[f"[[{c}]]" for c in extract_wikilinks(content)]}}}


def save_results(all_data, output_dir):
    output_dir = Path(output_dir)
    if output_dir.exists(): shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for fn, info in all_data.items():
        safe = re.sub(r'[<>:"/\\|?*]',"_",fn)
        if not safe.endswith(".md"): safe += ".md"
        content, meta = info.get("content",""), info.get("metadata",{})
        ft = meta.get("file_type","Note"); sf = TYPE_TO_FOLDER.get(ft,"Other")
        folder = output_dir / sf if sf else output_dir
        folder.mkdir(parents=True, exist_ok=True)
        fm = ["---",f"type: {ft}",f"source_chunk: {meta.get('source_chunk','?')}"]
        kc = meta.get("key_concepts",[])
        if kc: fm.append(f"key_concepts: {json.dumps(kc,ensure_ascii=False)}")
        concepts = meta.get("concepts",[])
        if concepts: fm.append(f"concepts: {json.dumps(concepts,ensure_ascii=False)}")
        fm.append("---\n")
        (folder/safe).write_text("\n".join(fm)+content, encoding="utf-8"); saved += 1
    log.info(f"✅ {saved}개 파일 → {output_dir}/"); return saved


def build_index(all_data, output_dir):
    output_dir = Path(output_dir)
    idx = {"generated_at":time.strftime("%Y-%m-%dT%H:%M:%S"),"total_files":len(all_data),
           "folder_structure":{},"files":{},"concept_index":{},"chunk_graph":{}}
    for fn,info in all_data.items():
        m = info.get("metadata",{}); ft = m.get("file_type","Note")
        sf = TYPE_TO_FOLDER.get(ft,"Other"); path = f"{sf}/{fn}" if sf else fn
        idx["files"][fn]={"path":path,"type":ft,"source_chunk":m.get("source_chunk",""),
            "key_concepts":m.get("key_concepts",[]),"concepts":m.get("concepts",[]),
            "content_length":len(info.get("content",""))}
        idx["folder_structure"].setdefault(sf if sf else "(root)",[]).append(fn)
        for c in m.get("concepts",[]):
            c2=c.strip("[]"); idx["concept_index"].setdefault(c2,[])
            if fn not in idx["concept_index"][c2]: idx["concept_index"][c2].append(fn)
        cid=m.get("source_chunk","").strip("[]")
        if cid: idx["chunk_graph"].setdefault(cid,[]); idx["chunk_graph"][cid].append({"file":fn,"type":ft})
    idx["stats"]={"by_type":{},"total_concepts":len(idx["concept_index"]),"total_chunks":len(idx["chunk_graph"])}
    for fi in idx["files"].values(): t=fi["type"]; idx["stats"]["by_type"][t]=idx["stats"]["by_type"].get(t,0)+1
    (output_dir/"vault_index.json").write_text(json.dumps(idx,ensure_ascii=False,indent=2),encoding="utf-8")
    log.info(f"📇 인덱스: {len(idx['files'])}개 파일, {len(idx['concept_index'])}개 개념"); return idx


def assemble_chunks(ocr_dir):
    ocr_dir = Path(ocr_dir)
    with open(ocr_dir/"chunks.json","r",encoding="utf-8") as f: chunks_raw = json.load(f)
    page_texts = load_page_texts(ocr_dir)
    chunks = []
    for c in chunks_raw["chunks"]:
        pn = expand_page_range(c["pages"]); tp = [page_texts.get(p,"") for p in pn]
        miss = [p for p in pn if p not in page_texts]
        if miss: print(f"  ⚠️ '{c['title']}': 누락 {miss}")
        chunks.append({"id":f"chunk_{len(chunks)+1:03d}","title":c["title"],
                       "text":"\n\n".join(tp),"pages":pn,"type":c.get("type","content")})
    return chunks


async def run_pipeline_async(cfg, book_name):
    ocr_dir = cfg.book_ocr_dir(book_name)
    vault_dir = cfg.book_vault_dir(book_name)

    chunks = assemble_chunks(ocr_dir)
    content_chunks = [c for c in chunks if c["type"]=="content"]
    toc_chunks = [c for c in chunks if c["type"]=="toc"]
    quiz_chunks = [c for c in chunks if c["type"]=="quiz"]
    heading_chunks = [c for c in chunks if c["type"]=="heading"]

    print(f"\n📖 {book_name}")
    print(f"   OCR: {ocr_dir}")
    print(f"   Vault: {vault_dir}")
    print(f"   Content:{len(content_chunks)} ToC:{len(toc_chunks)} Quiz:{len(quiz_chunks)} Heading:{len(heading_chunks)}")
    for c in chunks:
        pr=c["pages"]; ps=f"p{pr[0]}-{pr[-1]}" if len(pr)>1 else f"p{pr[0]}"
        print(f"  {c['id']} [{c['type']:7s}] {ps:10s} {len(c['text']):6,}자 ({len(pr)}p) | {c['title'][:40]}")

    client = AsyncOpenAI()
    sem = asyncio.Semaphore(cfg.get("pipeline.max_concurrent",5))
    all_results = {}
    def merge(data):
        for fn,info in data.items():
            name,ctr=fn,1
            while name in all_results: b,e=os.path.splitext(fn); name=f"{b}_{ctr}{e}"; ctr+=1
            all_results[name]=info

    log.info("="*60); log.info(f"📚 {book_name}"); log.info("="*60)

    log.info("① 원본 청크...")
    for chunk in chunks: merge(build_chunk_document(chunk))

    if content_chunks:
        log.info(f"② Content {len(content_chunks)}개")
        rl = await asyncio.gather(*[process_content_chunk(c,client,sem,cfg) for c in content_chunks], return_exceptions=True)
        for i,r in enumerate(rl):
            if isinstance(r,Exception): log.error(f"  ❌ {content_chunks[i]['id']}: {r}")
            elif isinstance(r,dict): merge(r); log.info(f"  ✅ {content_chunks[i]['id']}: {len(r)}개")

    if quiz_chunks:
        log.info(f"③ Quiz {len(quiz_chunks)}개")
        rl = await asyncio.gather(*[process_quiz_chunk(c,client,sem,cfg) for c in quiz_chunks], return_exceptions=True)
        for i,r in enumerate(rl):
            if isinstance(r,Exception): log.error(f"  ❌ {quiz_chunks[i]['id']}: {r}")
            elif isinstance(r,dict): merge(r)

    if toc_chunks: log.info(f"④ ToC {len(toc_chunks)}개 → 원본만")
    if heading_chunks: log.info(f"⑤ Heading {len(heading_chunks)}개 → 원본만")

    log.info("⑥ ToC..."); merge(generate_toc(chunks, all_results))
    log.info("⑦ 저장..."); saved = save_results(all_results, vault_dir)
    log.info("⑧ 인덱스..."); build_index(all_results, vault_dir)

    tc={}
    for info in all_results.values(): ft=info.get("metadata",{}).get("file_type","?"); tc[ft]=tc.get(ft,0)+1
    log.info("="*60); log.info(f"🎉 총 {saved}개")
    for t,c in sorted(tc.items()): log.info(f"  {t}: {c}개")
    log.info("="*60)
    return all_results, vault_dir


def run_pipeline(cfg, book_name):
    import nest_asyncio; nest_asyncio.apply()
    return asyncio.run(run_pipeline_async(cfg, book_name))
