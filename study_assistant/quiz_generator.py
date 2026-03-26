"""독립 퀴즈 생성기 — Vault의 .md를 소스로 새 퀴즈 생성."""

import json
import random
import re
import time
from pathlib import Path
from typing import List

from openai import OpenAI

from .config import Config
from .prompts import QUIZ_GEN_SYSTEM, QUIZ_GEN_TYPES, QUIZ_GEN_DIFFICULTY, QUIZ_GEN_SOURCE_INSTR
from .utils import JSONParser


def _load_md_folder(folder):
    files = []
    if not Path(folder).exists(): return files
    for p in sorted(Path(folder).glob("*.md")):
        text = p.read_text(encoding="utf-8")
        text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.DOTALL).strip()
        if len(text) > 50: files.append({"name": p.stem, "content": text})
    return files


def _pick_source(vault_dir, source, max_chars=15000):
    vault_dir = Path(vault_dir)
    summaries = _load_md_folder(vault_dir / "Summaries")
    quizzes = _load_md_folder(vault_dir / "Quizzes")
    if source == "summary": pool, st = summaries, "summary"
    elif source == "quiz": pool, st = quizzes, "quiz"
    else:
        opts = []
        if summaries: opts.append(("summary", summaries))
        if quizzes: opts.append(("quiz", quizzes))
        if not opts: raise FileNotFoundError(f"소스 없음: {vault_dir}")
        st, pool = random.choice(opts)
    if not pool: raise FileNotFoundError(f"{st} 파일 없음")
    random.shuffle(pool)
    text, names = "", []
    for f in pool:
        if len(text) + len(f["content"]) > max_chars and text: break
        text += f"\n\n--- {f['name']} ---\n\n{f['content']}"; names.append(f["name"])
    print(f"  📄 소스: {st} ({len(names)}개, {len(text):,}자)")
    for n in names: print(f"     - {n}")
    return st, text


def _print_quizzes(quizzes):
    for q in quizzes:
        print(f"\n{'━'*50}")
        print(f"[문제 {q.get('id','?')}] {q.get('type','?')}")
        print(f"Q. {q.get('question','')}")
        if q.get("type") == "multiple_choice":
            for opt in q.get("options",[]): print(f"  {opt}")
        print(f"\n✅ 정답: {q.get('answer','')}"); print(f"💡 해설: {q.get('explanation','')}")
    print(f"{'━'*50}")


def generate_quiz(cfg, book_name, n=5, quiz_type="multiple_choice",
                  source="random", difficulty="medium", save=True, print_result=True):
    """Vault의 Summaries/Quizzes .md → 새 퀴즈 → Generated_Quizzes/ 저장."""
    vault_dir = cfg.book_vault_dir(book_name)
    if not vault_dir.exists():
        raise FileNotFoundError(f"Vault 없음: {vault_dir}")
    if quiz_type not in QUIZ_GEN_TYPES:
        raise ValueError(f"유형: {list(QUIZ_GEN_TYPES.keys())}")

    model_config = cfg.get("quiz_generator.model", cfg.get("pipeline.quiz_create"))
    print(f"\n🎯 퀴즈 생성: {quiz_type} × {n} | {difficulty} | 소스: {source}")
    print(f"   모델: {model_config}")
    print(f"   Vault: {vault_dir}")

    st, text = _pick_source(vault_dir, source)

    # 프롬프트 조립
    system = f"{QUIZ_GEN_SYSTEM}\n\nFormat per question:\n{QUIZ_GEN_TYPES[quiz_type]}"
    dd = QUIZ_GEN_DIFFICULTY.get(difficulty, QUIZ_GEN_DIFFICULTY["medium"])
    si = QUIZ_GEN_SOURCE_INSTR.get(st, QUIZ_GEN_SOURCE_INSTR["summary"])
    user = f"{si}\n\n유형: {quiz_type}\n문제수: {n}\n난이도: {difficulty} — {dd}\n\n--- 원본 ---\n{text}"

    client = OpenAI(); print("  🔄 LLM...")
    try:
        resp = client.chat.completions.create(**model_config,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            response_format={"type":"json_object"})
        raw = JSONParser.parse(resp.choices[0].message.content)
    except Exception as e:
        print(f"  ❌ {e}"); return {"meta":{},"quizzes":[]}
    if not raw: print("  ❌ 파싱 실패"); return {"meta":{},"quizzes":[]}

    quizzes = raw.get("quizzes", raw if isinstance(raw, list) else [])
    result = {"meta":{"source":st,"quiz_type":quiz_type,"n":len(quizzes),
                      "difficulty":difficulty,"model":model_config.get("model","?"),
                      "generated_at":time.strftime("%Y-%m-%dT%H:%M:%S")},"quizzes":quizzes}
    print(f"  ✅ {len(quizzes)}개 생성")
    if print_result: _print_quizzes(quizzes)

    if save and quizzes:
        ts = time.strftime("%Y%m%d_%H%M%S")
        gd = vault_dir / "Generated_Quizzes"; gd.mkdir(parents=True, exist_ok=True)
        (gd / f"quiz_{quiz_type}_{difficulty}_{ts}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        for q in quizzes:
            md = f"## Question {q.get('id',0)}\n\nQ. {q.get('question','')}\n\n"
            if q.get("type")=="multiple_choice":
                for o in q.get("options",[]): md += f"- {o}\n"
                md += "\n"
            md += f"**정답:** {q.get('answer','')}\n\n### 해설\n\n{q.get('explanation','')}\n"
            (gd / f"GQ_{quiz_type}_{difficulty}_{ts}_Q{q.get('id',0)}.md").write_text(md, encoding="utf-8")
        print(f"  💾 {gd}/")
    return result