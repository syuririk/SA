"""1단계: Mistral OCR → 페이지별 .md 저장."""

import asyncio
import base64
import io
import math
import re
import time
from pathlib import Path
from typing import Tuple

try:
    from PyPDF2 import PdfReader, PdfWriter
    print('import pypdf : PyPDF2')
except ImportError:
    from pypdf import PdfReader, PdfWriter
    print('import pypdf : pypdf')

from mistralai.client import Mistral
print('import Mistral : mistralai.client')


from .config import Config
from .utils import safe_filename


def get_page_count(pdf_path: str) -> int:
    return len(PdfReader(pdf_path).pages)


def split_pdf(pdf_path: str, start: int, end: int) -> bytes:
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    for i in range(start, min(end + 1, len(reader.pages))):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _ocr_request(client, pdf_bytes: bytes, ocr_model: str):
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    return client.ocr.process(
        model=ocr_model,
        document={
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{pdf_b64}",
        },
        table_format=None,
        include_image_base64=True,
    )


async def run_batch_ocr(client, pdf_path, ocr_model, batch_size, max_concurrent):
    total = get_page_count(pdf_path)
    num_batches = math.ceil(total / batch_size)

    if num_batches == 1:
        print(f"  📄 전체 {total}p → 단일 요청")
        with open(pdf_path, "rb") as f:
            resp = _ocr_request(client, f.read(), ocr_model)
        return _sdk_pages_to_dicts(resp.pages, offset=0), total

    print(f"  📄 전체 {total}p → {num_batches}개 배치 (각 {batch_size}p)")
    sem = asyncio.Semaphore(max_concurrent)
    all_pages = []
    errors = []

    async def process_batch(idx, start, end):
        async with sem:
            chunk_bytes = split_pdf(pdf_path, start, end)
            size_mb = len(chunk_bytes) / (1024 * 1024)
            print(f"  🔄 배치 {idx+1}/{num_batches}: p{start}-{end} ({end-start+1}p, {size_mb:.1f}MB)")
            loop = asyncio.get_event_loop()
            try:
                resp = await loop.run_in_executor(None, _ocr_request, client, chunk_bytes, ocr_model)
                all_pages.extend(_sdk_pages_to_dicts(resp.pages, offset=start))
                print(f"  ✅ 배치 {idx+1}: {len(resp.pages)}p")
            except Exception as e:
                print(f"  ❌ 배치 {idx+1}: {e}")
                errors.append((idx, start, end, str(e)))

    batches = [(i, i * batch_size, min((i + 1) * batch_size - 1, total - 1))
               for i in range(num_batches)]
    await asyncio.gather(*[process_batch(i, s, e) for i, s, e in batches])

    if errors:
        print(f"\n⚠️ {len(errors)}개 배치 실패")
    all_pages.sort(key=lambda p: p["index"])
    print(f"  ✅ 재조립: {len(all_pages)}/{total}p")
    return all_pages, total


def _sdk_pages_to_dicts(pages, offset=0):
    result = []
    for page in pages:
        result.append({
            "index": offset + page.index,
            "markdown": page.markdown,
            "tables": page.tables if hasattr(page, "tables") and page.tables else [],
            "images": page.images if hasattr(page, "images") and page.images else [],
        })
    return result


def save_pages(book_name, pages, total, output_dir):
    safe_name = safe_filename(book_name)
    out = Path(output_dir) / safe_name
    out.mkdir(parents=True, exist_ok=True)

    for page in pages:
        pn = page["index"]
        md_text = page["markdown"]
        for tbl in page.get("tables", []):
            tid = tbl.get("id", "") if isinstance(tbl, dict) else getattr(tbl, "id", "")
            tc = tbl.get("content", "") if isinstance(tbl, dict) else getattr(tbl, "content", "")
            if tid and tc:
                md_text = md_text.replace(f"[{tid}]({tid})", tc)
        (out / f"page_{pn:04d}.md").write_text(
            f'---\nbook: "{book_name}"\npage: {pn}\n---\n\n# Page {pn}\n\n{md_text}\n',
            encoding="utf-8")

    all_md = f"# {book_name} — OCR Full Text\n\n"
    for md_path in sorted(out.glob("page_*.md")):
        raw = md_path.read_text(encoding="utf-8")
        body = re.sub(r'^---\n.*?\n---\n', '', raw, flags=re.DOTALL).strip()
        all_md += f"---\n{body}\n\n"
    (out / f"{safe_name}_full.md").write_text(all_md, encoding="utf-8")

    saved = len(list(out.glob("page_*.md")))
    print(f"✅ 저장: {out}/ ({saved}/{total}p + 통합본)")
    return out


def run_ocr(cfg, pdf_path, book_name):
    if Mistral is None:
        raise ImportError("mistralai 패키지를 설치하세요: pip install mistralai")

    import nest_asyncio
    nest_asyncio.apply()

    ocr_cfg = cfg.get("ocr")
    client = Mistral(api_key=cfg.get("api_keys.mistral", ""))
    total = get_page_count(pdf_path)
    output = cfg.output_dir

    print(f"\n📖 {book_name}: {total}p")
    t0 = time.time()

    if total <= ocr_cfg["batch_size"]:
        with open(pdf_path, "rb") as f:
            resp = _ocr_request(client, f.read(), ocr_cfg["model"])
        pages = _sdk_pages_to_dicts(resp.pages, offset=0)
    else:
        pages, total = asyncio.run(
            run_batch_ocr(client, pdf_path, ocr_cfg["model"],
                          ocr_cfg["batch_size"], ocr_cfg["max_concurrent"]))

    result = save_pages(book_name, pages, total, output)
    elapsed = time.time() - t0
    print(f"⏱️ {elapsed:.1f}초 ({total / max(elapsed, 1):.1f}p/s)")
    return result
