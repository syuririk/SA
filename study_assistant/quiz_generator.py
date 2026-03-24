"""독립 퀴즈 생성기 — obsidian_output의 .md를 소스로 새 퀴즈 생성."""

import json
import random
import re
import time
from pathlib import Path
from typing import List, Optional

from openai import OpenAI

from .config import Config
from .utils import JSONParser


# ── 퀴즈 유형별 포맷 ─────────────────────────────

QUIZ_TYPES = {
    "multiple_choice": (
        "Create multiple-choice questions with exactly 4 options.\n"
        "Format per question:\n"
        '{"id":N, "type":"multiple_choice", "question":"...", '
        '"options":["①...","②...","③...","④..."], "answer":"③...", "explanation":"..."}'
    ),
    "ox": (
        "Create True/False (O/X) questions.\n"
        "Format per question:\n"
        '{"id":N, "type":"ox", "question":"...", "answer":"O" or "X", "explanation":"..."}'
    ),
    "short_answer": (
        "Create short-answer questions (1-3 word keyword answers).\n"
        "Format per question:\n"
        '{"id":N, "type":"short_answer", "question":"...", "answer":"키워드", "explanation":"..."}'
    ),
    "fill_in_blank": (
        "Create fill-in-the-blank questions. Use ___ for the blank.\n"
        "Format per question:\n"
        '{"id":N, "type":"fill_in_blank", "question":"문장에 ___가 포함", "answer":"정답", "explanation":"..."}'
    ),
}

DIFFICULTY_DESC = {
    "easy": "기초 개념 확인 수준 (정의, 용어 구분)",
    "medium": "응용 문제 (개념 간 비교, 사례 적용)",
    "hard": "심화 문제 (복합 개념, 계산, 함정 보기 포함)",
}

SOURCE_INSTRUCTIONS = {
    "summary": "다음 요약 내용을 바탕으로 문제를 출제하세요.",
    "quiz": "다음 기존 문제를 참고하여, 유사하지만 새로운 문제를 만드세요. 기존 문제를 그대로 복사하지 마세요.",
}


# ── 내부 함수 ─────────────────────────────────────

def _load_md_folder(folder: Path) -> List[dict]:
    files = []
    if not folder.exists():
        return files
    for p in sorted(folder.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.DOTALL).strip()
        if len(text) > 50:
            files.append({"name": p.stem, "content": text})
    return files


def _pick_source(output_dir: Path, source: str, max_chars: int = 15000) -> tuple:
    summaries = _load_md_folder(output_dir / "Summaries")
    quizzes = _load_md_folder(output_dir / "Quizzes")

    if source == "summary":
        pool, src_type = summaries, "summary"
    elif source == "quiz":
        pool, src_type = quizzes, "quiz"
    else:
        options = []
        if summaries: options.append(("summary", summaries))
        if quizzes: options.append(("quiz", quizzes))
        if not options:
            raise FileNotFoundError(
                f"소스 없음: {output_dir / 'Summaries'}, {output_dir / 'Quizzes'}")
        src_type, pool = random.choice(options)

    if not pool:
        raise FileNotFoundError(f"{src_type} 소스 파일 없음")

    random.shuffle(pool)
    text, names = "", []
    for f in pool:
        if len(text) + len(f["content"]) > max_chars and text:
            break
        text += f"\n\n--- {f['name']} ---\n\n{f['content']}"
        names.append(f["name"])

    print(f"  📄 소스: {src_type} ({len(names)}개, {len(text):,}자)")
    for name in names:
        print(f"     - {name}")
    return src_type, text


def _print_quizzes(quizzes: list) -> None:
    for q in quizzes:
        print(f"\n{'━' * 50}")
        print(f"[문제 {q.get('id', '?')}] {q.get('type', '?')}")
        print(f"Q. {q.get('question', '')}")
        if q.get("type") == "multiple_choice":
            for opt in q.get("options", []):
                print(f"  {opt}")
        print(f"\n✅ 정답: {q.get('answer', '')}")
        print(f"💡 해설: {q.get('explanation', '')}")
    print(f"{'━' * 50}")


# ── 메인 함수 ─────────────────────────────────────

def generate_quiz(
    cfg: Config,
    book_dir,
    n: int = 5,
    quiz_type: str = "multiple_choice",
    source: str = "random",
    difficulty: str = "medium",
    save: bool = True,
    print_result: bool = True,
) -> dict:
    """퀴즈 생성.

    소스: book_dir/obsidian_output/Summaries/*.md, Quizzes/*.md
    저장: book_dir/obsidian_output/Generated_Quizzes/

    Args:
        cfg: Config 객체 (quiz_generator 설정 참조)
        book_dir: 교재 디렉토리
        n: 문제 수
        quiz_type: multiple_choice / ox / short_answer / fill_in_blank
        source: summary / quiz / random
        difficulty: easy / medium / hard
    """
    book_dir = Path(book_dir)
    output_dir = book_dir / "obsidian_output"

    if quiz_type not in QUIZ_TYPES:
        raise ValueError(f"지원하지 않는 유형: {quiz_type}. 가능: {list(QUIZ_TYPES.keys())}")

    model_config = cfg.get("quiz_generator.model", cfg.get("pipeline.quiz_create"))

    print(f"\n🎯 퀴즈 생성")
    print(f"   유형: {quiz_type} | 문제수: {n} | 난이도: {difficulty} | 소스: {source}")
    print(f"   모델: {model_config}")
    print(f"   소스 경로: {output_dir}")

    # 1. 소스 선택
    src_type, src_text = _pick_source(output_dir, source)

    # 2. 프롬프트
    system = (
        "You are a certification exam question writer.\n"
        "You MUST respond with ONLY raw JSON. No markdown fences, no preamble.\n"
        "Do NOT include meta-commentary about the output itself.\n"
        "LANGUAGE: Write in the SAME language as the source material.\n\n"
        f"Quiz format:\n{QUIZ_TYPES[quiz_type]}\n\n"
        'Wrap output in: {"quizzes": [...]}'
    )

    diff_desc = DIFFICULTY_DESC.get(difficulty, DIFFICULTY_DESC["medium"])
    src_instr = SOURCE_INSTRUCTIONS.get(src_type, SOURCE_INSTRUCTIONS["summary"])

    user = (
        f"{src_instr}\n\n"
        f"퀴즈 유형: {quiz_type}\n"
        f"문제 수: {n}개\n"
        f"난이도: {difficulty} — {diff_desc}\n\n"
        f"--- 원본 내용 ---\n{src_text}"
    )

    # 3. LLM 호출
    client = OpenAI()
    print(f"  🔄 LLM 호출...")
    try:
        kwargs = {
            **model_config,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        response = client.chat.completions.create(**kwargs)
        raw = JSONParser.parse(response.choices[0].message.content)
    except Exception as e:
        print(f"  ❌ LLM 오류: {e}")
        return {"meta": {}, "quizzes": []}

    if raw is None:
        print("  ❌ 파싱 실패")
        return {"meta": {}, "quizzes": []}

    quizzes = raw.get("quizzes", [])
    if not quizzes and isinstance(raw, list):
        quizzes = raw

    result = {
        "meta": {
            "source": src_type,
            "quiz_type": quiz_type,
            "n": len(quizzes),
            "difficulty": difficulty,
            "model": model_config.get("model", "unknown"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "quizzes": quizzes,
    }

    print(f"  ✅ {len(quizzes)}개 문제 생성")

    # 4. 출력
    if print_result:
        _print_quizzes(quizzes)

    # 5. 저장
    if save and quizzes:
        ts = time.strftime("%Y%m%d_%H%M%S")
        gen_dir = output_dir / "Generated_Quizzes"
        gen_dir.mkdir(parents=True, exist_ok=True)

        # JSON
        json_path = gen_dir / f"quiz_{quiz_type}_{difficulty}_{ts}.json"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        # 개별 .md
        for q in quizzes:
            qid = q.get("id", 0)
            md_name = f"GQ_{quiz_type}_{difficulty}_{ts}_Q{qid}.md"
            md = f"## Question {qid}\n\nQ. {q.get('question', '')}\n\n"
            if q.get("type") == "multiple_choice":
                for opt in q.get("options", []):
                    md += f"- {opt}\n"
                md += "\n"
            md += f"**정답:** {q.get('answer', '')}\n\n"
            md += f"### 해설\n\n{q.get('explanation', '')}\n"
            (gen_dir / md_name).write_text(md, encoding="utf-8")

        print(f"  💾 저장: {gen_dir}/")

    return result