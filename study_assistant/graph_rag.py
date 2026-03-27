"""Graph RAG — Formula-centric entity extraction, graph build, visualization."""

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from openai import AsyncOpenAI, OpenAI

from .config import Config
from .prompts import ENTITY_EXTRACT
from .utils import JSONParser


# ══════════════════════════════════════════════════
# 1. Entity Extraction (async, parallel)
# ══════════════════════════════════════════════════

def _load_source_files(vault_dir: Path, content_only: bool = True) -> List[dict]:
    """Load .md files from vault.

    Args:
        content_only: True면 Sources/ 폴더의 type: Source 청크만 로드.
                      False면 Sources/ + Summaries/ 전체 로드.
    """
    files = []

    if content_only:
        folder = vault_dir / "Sources"
        if folder.exists():
            for p in sorted(folder.glob("*.md")):
                raw = p.read_text(encoding="utf-8")
                fm_match = re.match(r'^---\n(.*?)\n---\n', raw, re.DOTALL)
                if fm_match and "type: Source" not in fm_match.group(1):
                    continue
                text = re.sub(r'^---\n.*?\n---\n', '', raw, flags=re.DOTALL).strip()
                if len(text) > 50:
                    files.append({"name": p.stem, "folder": "Sources", "content": text})
    else:
        for subfolder in ["Sources", "Summaries"]:
            folder = vault_dir / subfolder
            if not folder.exists():
                continue
            for p in sorted(folder.glob("*.md")):
                text = p.read_text(encoding="utf-8")
                text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.DOTALL).strip()
                if len(text) > 50:
                    files.append({"name": p.stem, "folder": subfolder, "content": text})

    return files


async def _extract_one(
    client: AsyncOpenAI,
    sem: asyncio.Semaphore,
    f: dict,
    model_config: dict,
    max_retries: int = 3,
) -> dict:
    """단일 파일에서 entities 추출 (비동기)."""
    user_prompt = (
        f"Source: {f['name']} ({f['folder']})\n\n"
        f"Material:\n{f['content'][:12000]}"
    )

    async with sem:
        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.chat.completions.create(
                    **model_config,
                    messages=[
                        {"role": "system", "content": ENTITY_EXTRACT},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                )
                raw = JSONParser.parse(resp.choices[0].message.content)
                if raw is not None:
                    return {"file": f, "raw": raw}
                print(f"  ⚠️  {f['name']}: 파싱 실패 (시도 {attempt}/{max_retries})")
            except Exception as e:
                print(f"  ❌ {f['name']}: {e} (시도 {attempt}/{max_retries})")
            if attempt < max_retries:
                await asyncio.sleep(2 * attempt)

    return {"file": f, "raw": None}


async def _extract_entities_async(
    cfg: Config,
    book_name: str,
    content_only: bool,
) -> dict:
    vault_dir = cfg.book_vault_dir(book_name)
    model_config = cfg.get("graph_rag.model", cfg.get("pipeline.summary"))
    max_concurrent = cfg.get("graph_rag.max_concurrent",
                             cfg.get("pipeline.max_concurrent", 5))

    files = _load_source_files(vault_dir, content_only=content_only)
    if not files:
        raise FileNotFoundError(
            f"처리할 파일 없음: {vault_dir}\n"
            f"  (content_only={content_only}) — 파이프라인을 먼저 실행했는지 확인하세요."
        )

    model_name = model_config.get("model") if isinstance(model_config, dict) else model_config
    print(f"\n🔍 Entity Extraction: {book_name}")
    print(f"   Model:     {model_name}")
    print(f"   파일 수:   {len(files)}개  (content_only={content_only})")
    print(f"   동시 실행: {max_concurrent}개")

    client = AsyncOpenAI()
    sem = asyncio.Semaphore(max_concurrent)

    tasks = [_extract_one(client, sem, f, model_config) for f in files]
    results = await asyncio.gather(*tasks)

    # ── 결과 수집 ──────────────────────────────────────────
    all_formulas: List[dict] = []
    all_variables: List[dict] = []
    all_concepts: List[dict] = []
    formula_counter = 0

    for res in results:
        f, raw = res["file"], res["raw"]
        if raw is None:
            print(f"  ⛔ {f['name']}: 건너뜀")
            continue

        formulas = raw.get("formulas", [])
        for fm in formulas:
            formula_counter += 1
            fm.setdefault("id", f"f_{formula_counter:03d}")
            fm.setdefault("source_chunks", [])
            fm["source_chunks"].append(f["name"])
            fm.setdefault("variables", [])
            fm.setdefault("name", "")
            fm.setdefault("latex", "")
            fm.setdefault("description", "")

        variables = raw.get("variables", [])
        for v in variables:
            v.setdefault("symbol", "")
            v.setdefault("name", "")
            v.setdefault("used_in", [])

        concepts = raw.get("concepts", [])
        for c in concepts:
            c.setdefault("name", "")
            c.setdefault("related_formulas", [])
            c.setdefault("prerequisites", [])

        all_formulas.extend(formulas)
        all_variables.extend(variables)
        all_concepts.extend(concepts)
        print(f"  ✅ {f['folder']}/{f['name']}: "
              f"{len(formulas)}F {len(variables)}V {len(concepts)}C")

    print(f"\n  합계: {len(all_formulas)} formulas, "
          f"{len(all_variables)} variables, {len(all_concepts)} concepts")

    # ── 중복 제거 ──────────────────────────────────────────
    var_map: Dict[str, dict] = {}
    for v in all_variables:
        sym = v["symbol"]
        if not sym:
            continue
        if sym in var_map:
            var_map[sym]["used_in"] = list(
                set(var_map[sym]["used_in"] + v.get("used_in", [])))
        else:
            var_map[sym] = v
    all_variables = list(var_map.values())

    concept_map: Dict[str, dict] = {}
    for c in all_concepts:
        name = c["name"]
        if not name:
            continue
        if name in concept_map:
            concept_map[name]["related_formulas"] = list(
                set(concept_map[name]["related_formulas"] + c.get("related_formulas", [])))
            concept_map[name]["prerequisites"] = list(
                set(concept_map[name]["prerequisites"] + c.get("prerequisites", [])))
        else:
            concept_map[name] = c
    all_concepts = list(concept_map.values())

    graph = {
        "book_name": book_name,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "content_only": content_only,
        "entities": {
            "formulas": all_formulas,
            "variables": all_variables,
            "concepts": all_concepts,
        },
        "edges": [],
    }

    gpath = vault_dir / "formula_graph.json"
    gpath.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ Entities 저장: {gpath}")

    return graph


def extract_entities(cfg: Config, book_name: str, content_only: bool = True) -> dict:
    """Extract formulas, variables, concepts from vault .md files (병렬 실행).

    Args:
        cfg: Config 객체
        book_name: 교재 이름
        content_only: True(기본)면 content 타입 청크(Sources/)만 처리.
                      False면 Sources/ + Summaries/ 전체 처리.

    Saves: Vault/{book}/formula_graph.json
    """
    import nest_asyncio
    nest_asyncio.apply()
    return asyncio.run(_extract_entities_async(cfg, book_name, content_only))


# ══════════════════════════════════════════════════
# 2. Graph Build (edges)
# ══════════════════════════════════════════════════

def build_graph(cfg: Config, book_name: str, graph: Optional[dict] = None) -> dict:
    """Build edges from entities. Saves formula_graph.json."""
    vault_dir = cfg.book_vault_dir(book_name)

    if graph is None:
        gpath = vault_dir / "formula_graph.json"
        if gpath.exists():
            with open(gpath, "r", encoding="utf-8") as f:
                graph = json.load(f)
        else:
            graph = extract_entities(cfg, book_name)

    formulas = graph["entities"]["formulas"]
    variables = graph["entities"]["variables"]
    concepts = graph["entities"]["concepts"]
    edges = []

    # Formula → Variable (USES)
    for fm in formulas:
        for var_sym in fm.get("variables", []):
            edges.append({"from": fm["id"], "to": f"v_{var_sym}", "type": "USES"})

    # Chunk → Formula (CONTAINS)
    for fm in formulas:
        for src in fm.get("source_chunks", []):
            edges.append({"from": src, "to": fm["id"], "type": "CONTAINS"})

    # Concept → Formula (RELATED_TO)
    for c in concepts:
        for fid in c.get("related_formulas", []):
            edges.append({"from": f"c_{c['name']}", "to": fid, "type": "RELATED_TO"})

    # Concept → Concept (PREREQUISITE)
    for c in concepts:
        for prereq in c.get("prerequisites", []):
            edges.append({"from": f"c_{prereq}", "to": f"c_{c['name']}", "type": "PREREQUISITE"})

    # 같은 변수를 공유하는 수식끼리 연결 (SHARES_VARIABLE)
    var_to_formulas: Dict[str, List[str]] = {}
    for fm in formulas:
        for v in fm.get("variables", []):
            var_to_formulas.setdefault(v, []).append(fm["id"])
    for var_sym, fids in var_to_formulas.items():
        if len(fids) > 1:
            for i in range(len(fids)):
                for j in range(i + 1, len(fids)):
                    edges.append({
                        "from": fids[i], "to": fids[j],
                        "type": "SHARES_VARIABLE", "variable": var_sym,
                    })

    graph["edges"] = edges

    gpath = vault_dir / "formula_graph.json"
    gpath.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ Graph 저장: {gpath}")
    print(f"   {len(formulas)} formulas, {len(variables)} variables, "
          f"{len(concepts)} concepts, {len(edges)} edges")

    return graph


# ══════════════════════════════════════════════════
# 3. Visualization — Pyvis
# ══════════════════════════════════════════════════

def visualize_graph(cfg: Config, book_name: str, graph: Optional[dict] = None,
                    output_name: str = "formula_graph.html") -> Path:
    """Generate interactive Pyvis HTML visualization."""
    try:
        from pyvis.network import Network
    except ImportError:
        raise ImportError("pip install pyvis")

    vault_dir = cfg.book_vault_dir(book_name)
    if graph is None:
        gpath = vault_dir / "formula_graph.json"
        with open(gpath, "r", encoding="utf-8") as f:
            graph = json.load(f)

    net = Network(height="700px", width="100%", bgcolor="#1a1a2e", font_color="white",
                  directed=True, notebook=True, cdn_resources="remote")
    net.barnes_hut(gravity=-3000, central_gravity=0.3, spring_length=150)

    formulas = graph["entities"]["formulas"]
    variables = graph["entities"]["variables"]
    concepts = graph["entities"]["concepts"]
    edges = graph["edges"]

    for fm in formulas:
        label = fm.get("name", fm["id"])
        title = f"📐 {fm.get('name','')}\n{fm.get('latex','')}\n\n{fm.get('description','')}"
        net.add_node(fm["id"], label=label, title=title,
                     color="#4fc3f7", shape="box", size=25,
                     font={"size": 14, "color": "white"})

    added_vars = set()
    for v in variables:
        vid = f"v_{v['symbol']}"
        if vid not in added_vars:
            title = f"📊 {v['symbol']}: {v.get('name','')}\n{v.get('description','')}"
            net.add_node(vid, label=v["symbol"], title=title,
                         color="#66bb6a", shape="ellipse", size=15,
                         font={"size": 12, "color": "white"})
            added_vars.add(vid)

    for c in concepts:
        cid = f"c_{c['name']}"
        title = f"💡 {c['name']}\nFormulas: {', '.join(c.get('related_formulas',[]))}"
        net.add_node(cid, label=c["name"], title=title,
                     color="#ffd54f", shape="diamond", size=20,
                     font={"size": 13, "color": "#333"})

    edge_colors = {
        "USES": "#81c784", "CONTAINS": "#90caf9", "RELATED_TO": "#fff176",
        "PREREQUISITE": "#ef5350", "SHARES_VARIABLE": "#ce93d8", "DERIVED_FROM": "#ffab91",
    }
    for e in edges:
        color = edge_colors.get(e["type"], "#888")
        title = e["type"]
        if e.get("variable"):
            title += f" ({e['variable']})"
        try:
            dashes = e["type"] in ("PREREQUISITE", "SHARES_VARIABLE", "DERIVED_FROM")
            net.add_edge(e["from"], e["to"], title=title, color=color,
                         dashes=dashes, arrows="to", width=1.5)
        except Exception:
            pass

    out_path = vault_dir / output_name
    net.save_graph(str(out_path))
    print(f"\n📊 Visualization: {out_path}")
    return out_path


# ══════════════════════════════════════════════════
# 4. Obsidian Export — .md with [[wikilinks]]
# ══════════════════════════════════════════════════

def export_obsidian_graph(cfg: Config, book_name: str, graph: Optional[dict] = None) -> Path:
    """Export graph as Obsidian .md files with [[wikilinks]] for Graph View."""
    vault_dir = cfg.book_vault_dir(book_name)
    if graph is None:
        gpath = vault_dir / "formula_graph.json"
        with open(gpath, "r", encoding="utf-8") as f:
            graph = json.load(f)

    graph_dir = vault_dir / "Graph"
    graph_dir.mkdir(parents=True, exist_ok=True)

    formulas = graph["entities"]["formulas"]
    variables = graph["entities"]["variables"]
    concepts = graph["entities"]["concepts"]

    for fm in formulas:
        md = f"---\ntype: Formula\nid: {fm['id']}\n---\n\n"
        md += f"# {fm.get('name', fm['id'])}\n\n"
        md += f"$$\n{fm.get('latex', '')}\n$$\n\n"
        if fm.get("description"):
            md += f"{fm['description']}\n\n"
        md += "## Variables\n\n"
        for v in fm.get("variables", []):
            md += f"- [[Var_{v}]]\n"
        md += "\n## Source\n\n"
        for src in fm.get("source_chunks", []):
            md += f"- [[{src}]]\n"
        related_concepts = [c for c in concepts if fm["id"] in c.get("related_formulas", [])]
        if related_concepts:
            md += "\n## Concepts\n\n"
            for c in related_concepts:
                md += f"- [[Concept_{c['name']}]]\n"
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', fm.get("name", fm["id"]))
        (graph_dir / f"Formula_{safe_name}.md").write_text(md, encoding="utf-8")

    for v in variables:
        md = f"---\ntype: Variable\nsymbol: {v['symbol']}\n---\n\n"
        md += f"# {v['symbol']}: {v.get('name', '')}\n\n"
        if v.get("description"):
            md += f"{v['description']}\n\n"
        if v.get("unit"):
            md += f"**Unit:** {v['unit']}\n\n"
        using = [fm for fm in formulas if v["symbol"] in fm.get("variables", [])]
        if using:
            md += "## Used in\n\n"
            for fm in using:
                safe = re.sub(r'[<>:"/\\|?*]', '_', fm.get("name", fm["id"]))
                md += f"- [[Formula_{safe}]]\n"
        (graph_dir / f"Var_{v['symbol']}.md").write_text(md, encoding="utf-8")

    for c in concepts:
        md = f"---\ntype: Concept\n---\n\n"
        md += f"# {c['name']}\n\n"
        if c.get("related_formulas"):
            md += "## Related Formulas\n\n"
            for fid in c["related_formulas"]:
                fm = next((f for f in formulas if f["id"] == fid), None)
                if fm:
                    safe = re.sub(r'[<>:"/\\|?*]', '_', fm.get("name", fm["id"]))
                    md += f"- [[Formula_{safe}]]: ${fm.get('latex', '')}$\n"
                else:
                    md += f"- {fid}\n"
        if c.get("prerequisites"):
            md += "\n## Prerequisites\n\n"
            for prereq in c["prerequisites"]:
                md += f"- [[Concept_{prereq}]]\n"
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', c["name"])
        (graph_dir / f"Concept_{safe_name}.md").write_text(md, encoding="utf-8")

    md = "# 📐 Formula Graph\n\n"
    md += "## Formulas\n\n"
    for fm in formulas:
        safe = re.sub(r'[<>:"/\\|?*]', '_', fm.get("name", fm["id"]))
        md += f"- [[Formula_{safe}]]: ${fm.get('latex', '')}$\n"
    md += "\n## Concepts\n\n"
    for c in concepts:
        safe = re.sub(r'[<>:"/\\|?*]', '_', c["name"])
        md += f"- [[Concept_{safe}]]\n"
    md += "\n## Variables\n\n"
    for v in variables:
        md += f"- [[Var_{v['symbol']}]]: {v.get('name', '')}\n"
    (graph_dir / "Formula_Graph_Index.md").write_text(md, encoding="utf-8")

    total = len(formulas) + len(variables) + len(concepts) + 1
    print(f"\n📚 Obsidian Graph 내보내기: {graph_dir}/ ({total} files)")
    return graph_dir


# ══════════════════════════════════════════════════
# 5. Query
# ══════════════════════════════════════════════════

def query_formulas(graph: dict, query: str) -> str:
    """Search graph by concept/variable/formula name → context string for prompts."""
    query_lower = query.lower()
    results = []

    for fm in graph["entities"]["formulas"]:
        if (query_lower in fm.get("name", "").lower() or
                query_lower in fm.get("latex", "").lower() or
                any(query_lower == v.lower() for v in fm.get("variables", []))):
            results.append(
                f"Formula: {fm.get('name','')}\n"
                f"  LaTeX: {fm.get('latex','')}\n"
                f"  Variables: {', '.join(fm.get('variables',[]))}\n"
                f"  Description: {fm.get('description','')}\n"
                f"  Source: {', '.join(fm.get('source_chunks',[]))}"
            )

    for c in graph["entities"]["concepts"]:
        if query_lower in c.get("name", "").lower():
            results.append(
                f"Concept: {c['name']}\n"
                f"  Related formulas: {', '.join(c.get('related_formulas', []))}\n"
                f"  Prerequisites: {', '.join(c.get('prerequisites', []))}"
            )

    return "\n\n".join(results) if results else f"No results for: {query}"


def load_graph(cfg: Config, book_name: str) -> Optional[dict]:
    """Load formula_graph.json if exists."""
    gpath = cfg.book_vault_dir(book_name) / "formula_graph.json"
    if gpath.exists():
        with open(gpath, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# ══════════════════════════════════════════════════
# 6. Main entry point
# ══════════════════════════════════════════════════

def run_graph_rag(
    cfg: Config,
    book_name: str,
    visualize: bool = True,
    obsidian_export: bool = True,
    content_only: bool = True,
) -> dict:
    """Full pipeline: extract → build → visualize → obsidian export.

    Args:
        cfg: Config 객체
        book_name: 교재 이름
        visualize: pyvis HTML 시각화 생성 여부
        obsidian_export: Obsidian .md 내보내기 여부
        content_only: True(기본)면 content 타입 청크(Sources/)만 병렬 처리.
                      False면 Sources/ + Summaries/ 전체 처리.
    """
    print(f"\n{'═' * 50}")
    print(f"📐 Graph RAG: {book_name}")
    print(f"{'═' * 50}")

    graph = extract_entities(cfg, book_name, content_only=content_only)
    graph = build_graph(cfg, book_name, graph)

    if visualize:
        try:
            visualize_graph(cfg, book_name, graph)
        except ImportError:
            print("  ⚠️ pyvis 미설치 — 시각화 건너뜀")

    if obsidian_export:
        export_obsidian_graph(cfg, book_name, graph)

    print(f"\n{'═' * 50}")
    print(f"✅ Graph RAG 완료")
    print(f"{'═' * 50}")
    return graph