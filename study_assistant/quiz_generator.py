"""독립 퀴즈 생성기 — obsidian_output의 Summary/Quiz .md를 소스로 활용."""

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from openai import OpenAI

from .config import Config
from .utils import JSONParser


# ── 퀴즈 유형별 시스템 프롬프트 ──────────────────────

_SYSTEM_BASE = (
    "You are a certification exam question writer.\n"
    "You MUST respond with ONLY raw JSON. No markdown fences, no preamble.\n"
    "Do NOT include meta-commentary about the output itself.\n"
    "LANGUAGE: Write in the SAME language as the source material."
)

_TYPE_INSTRUCTIONS = {
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

_DIFFICULTY_DESC = {
    "easy": "기초 개념 확인 수준 (정의, 용어 구분)",
    "medium": "응용 문제 (개념 간 비교, 사례 적용)",
    "hard": "심화 문제 (복합 개념, 계산, 함정 보기 포함)",
}

_SOURCE_INSTRUCTIONS = {
    "summary": "다음 요약 내용을 바탕으로 문제를 출제하세요.",
    "quiz": "다음 기존 문제를 참고하여, 유사하지만 새로운 문제를 만드세요. 기존 문제를 그대로 복사하지 마세요.",
}


# ── 소스 로드 ────────────────────────────────────

def _load_md_files(folder: Path) -> List[dict]:
    """폴더 내 .md 파일들을 로드하여 {name, content} 리스트 반환."""
    files = []
    if not folder.exists():
        return files
    for p in sorted(folder.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        # frontmatter 제거
        text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.DOTALL).strip()
        if len(text) > 50:  # 너무 짧은 파일 스킵
            files.append({"name": p.stem, "content": text})
    return files


def pick_source(
    output_dir: Path,
    source: str = "random",
    max_chars: int = 15000,
) -> tuple:
    """소스 텍스트를 선택하여 (source_type, text) 반환.

    source: "summary" | "quiz" | "random"
    """
    summaries = _load_md_files(output_dir / "Summaries")
    quizzes = _load_md_files(output_dir / "Quizzes")

    if source == "summary":
        pool, src_type = summaries, "summary"
    elif source == "quiz":
        pool, src_type = quizzes, "quiz"
    else:  # random
        options = []
        if summaries:
            options.append(("summary", summaries))
        if quizzes:
            options.append(("quiz", quizzes))
        if not options:
            raise FileNotFoundError("Summaries/와 Quizzes/ 모두 비어있습니다")
        src_type, pool = random.choice(options)

    if not pool:
        raise FileNotFoundError(f"{src_type} 소스 파일이 없습니다")

    # 랜덤 파일 선택 + max_chars 제한
    random.shuffle(pool)
    selected_text = ""
    selected_names = []
    for f in pool:
        if len(selected_text) + len(f["content"]) > max_chars:
            if selected_text:  # 이미 충분하면 중단
                break
        selected_text += f"\n\n--- {f['name']} ---\n\n{f['content']}"
        selected_names.append(f["name"])

    print(f"  📄 소스: {src_type} ({len(selected_names)}개 파일, {len(selected_text):,}자)")
    return src_type, selected_text


# ── 프롬프트 빌더 ────────────────────────────────

def _build_prompt(
    source_type: str,
    source_text: str,
    quiz_type: str,
    n: int,
    difficulty: str,
) -> tuple:
    """(system_prompt, user_prompt) 튜플 반환."""
    system = (
        f"{_SYSTEM_BASE}\n\n"
        f"Quiz format:\n{_TYPE_INSTRUCTIONS[quiz_type]}\n\n"
        'Wrap output in: {{"quizzes": [...]}}'
    )

    diff_desc = _DIFFICULTY_DESC.get(difficulty, _DIFFICULTY_DESC["medium"])
    source_instr = _SOURCE_INSTRUCTIONS.get(source_type, _SOURCE_INSTRUCTIONS["summary"])

    user = (
        f"{source_instr}\n\n"
        f"퀴즈 유형: {quiz_type}\n"
        f"문제 수: {n}개\n"
        f"난이도: {difficulty} — {diff_desc}\n\n"
        f"--- 원본 내용 ---\n{source_text}"
    )

    return system, user


# ── LLM 호출 ─────────────────────────────────────

def _call_llm(system: str, user: str, model_config: dict) -> Optional[dict]:
    """동기 LLM 호출."""
    client = OpenAI()
    kwargs = {
        **model_config,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        response = client.chat.completions.create(**kwargs)
        return JSONParser.parse(response.choices[0].message.content)
    except Exception as e:
        print(f"  ❌ LLM 오류: {e}")
        return None


# ── 출력 ──────────────────────────────────────────

def _print_quizzes(quizzes: List[dict]) -> None:
    """퀴즈를 보기 좋게 출력."""
    for q in quizzes:
        qid = q.get("id", "?")
        qtype = q.get("type", "?")
        print(f"\n{'━' * 50}")
        print(f"[문제 {qid}] {qtype}")
        print(f"Q. {q.get('question', '')}")

        if qtype == "multiple_choice":
            for opt in q.get("options", []):
                print(f"  {opt}")

        print(f"\n✅ 정답: {q.get('answer', '')}")
        print(f"💡 해설: {q.get('explanation', '')}")
    print(f"{'━' * 50}")


def _save_json(result: dict, output_dir: Path) -> Path:
    """결과를 JSON 파일로 저장."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"generated_quiz_{ts}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  💾 저장: {path}")
    return path


# ── 메인 함수 ─────────────────────────────────────

def generate_quiz(
    cfg: Config,
    book_dir: Path,
    n: int = 5,
    quiz_type: str = "multiple_choice",
    source: str = "random",
    difficulty: str = "medium",
    save_json: bool = True,
    print_result: bool = True,
) -> dict:
    """퀴즈 생성 메인 함수.

    Args:
        cfg: Config 객체
        book_dir: 교재 디렉토리 (obsidian_output/ 하위에 Summaries/, Quizzes/ 필요)
        n: 생성할 문제 수
        quiz_type: "multiple_choice" | "ox" | "short_answer" | "fill_in_blank"
        source: "summary" | "quiz" | "random"
        difficulty: "easy" | "medium" | "hard"
        save_json: JSON 파일 저장 여부
        print_result: 콘솔 출력 여부

    Returns:
        {"meta": {...}, "quizzes": [...]}
    """
    book_dir = Path(book_dir)
    output_dir = book_dir / "obsidian_output"

    if quiz_type not in _TYPE_INSTRUCTIONS:
        raise ValueError(f"지원하지 않는 퀴즈 유형: {quiz_type}. "
                         f"가능: {list(_TYPE_INSTRUCTIONS.keys())}")

    print(f"\n🎯 퀴즈 생성: {quiz_type} × {n}문제 | 난이도: {difficulty} | 소스: {source}")

    # 1. 소스 선택
    src_type, src_text = pick_source(output_dir, source)

    # 2. 프롬프트 빌드
    system, user = _build_prompt(src_type, src_text, quiz_type, n, difficulty)

    # 3. LLM 호출
    model_config = cfg.get("quiz_generator.model", cfg.get("pipeline.quiz_create"))
    print(f"  🤖 모델: {model_config}")
    raw = _call_llm(system, user, model_config)

    if raw is None:
        print("  ❌ 퀴즈 생성 실패")
        return {"meta": {}, "quizzes": []}

    quizzes = raw.get("quizzes", [])
    if not quizzes and isinstance(raw, list):
        quizzes = raw

    # 4. 결과 조립
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

    # 5. 출력
    if print_result:
        _print_quizzes(quizzes)

    # 6. 저장
    if save_json:
        _save_json(result, output_dir)

    return result