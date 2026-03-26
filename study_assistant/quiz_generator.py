"""Standalone quiz generator — uses Vault .md files as source."""

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
    """Load .md files from a folder, strip frontmatter."""
    files = []
    if not Path(folder).exists():
        return files
    for p in sorted(Path(folder).glob("*.md")):
        text = p.read_text(encoding="utf-8")
        text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.DOTALL).strip()
        if len(text) > 50:
            files.append({"name": p.stem, "content": text})
    return files


def _pick_source(vault_dir, source, max_chars=15000):
    """Select source text from Summaries or Quizzes folder."""
    vault_dir = Path(vault_dir)
    summaries = _load_md_folder(vault_dir / "Summaries")
    quizzes = _load_md_folder(vault_dir / "Quizzes")

    if source == "summary":
        pool, src_type = summaries, "summary"
    elif source == "quiz":
        pool, src_type = quizzes, "quiz"
    else:
        opts = []
        if summaries:
            opts.append(("summary", summaries))
        if quizzes:
            opts.append(("quiz", quizzes))
        if not opts:
            raise FileNotFoundError(f"No source files in: {vault_dir}")
        src_type, pool = random.choice(opts)

    if not pool:
        raise FileNotFoundError(f"No {src_type} files found")

    random.shuffle(pool)
    text, names = "", []
    for f in pool:
        if len(text) + len(f["content"]) > max_chars and text:
            break
        text += f"\n\n--- {f['name']} ---\n\n{f['content']}"
        names.append(f["name"])

    print(f"  📄 Source: {src_type} ({len(names)} files, {len(text):,} chars)")
    for n in names:
        print(f"     - {n}")
    return src_type, text, names


def _normalize_quiz(q, idx, quiz_type, source_files):
    """Ensure every quiz dict has all required fields with defaults."""
    return {
        "id": q.get("id", idx),
        "type": q.get("type", quiz_type),
        "question": q.get("question", ""),
        "options": q.get("options", []) if quiz_type == "multiple_choice" else [],
        "answer": q.get("answer", ""),
        "explanation": q.get("explanation", ""),
        "key_concepts": q.get("key_concepts", []),
        "source": q.get("source", ", ".join(source_files) if source_files else "unknown"),
    }


def _print_quizzes(quizzes):
    """Pretty-print quiz list."""
    for q in quizzes:
        print(f"\n{'━' * 50}")
        print(f"[Q{q['id']}] {q['type']}")
        print(f"Q. {q['question']}")

        if q["type"] == "multiple_choice" and q["options"]:
            for opt in q["options"]:
                print(f"  {opt}")

        print(f"\n✅ Answer: {q['answer']}")
        print(f"💡 Explanation: {q['explanation']}")

        if q["key_concepts"]:
            print(f"🔑 Concepts: {', '.join(q['key_concepts'])}")
        if q["source"]:
            print(f"📄 Source: {q['source']}")
    print(f"{'━' * 50}")


def _save_quizzes(quizzes, result, vault_dir, quiz_type, difficulty):
    """Save quizzes as JSON + individual .md files."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    gen_dir = Path(vault_dir) / "Generated_Quizzes"
    gen_dir.mkdir(parents=True, exist_ok=True)

    # JSON
    json_path = gen_dir / f"quiz_{quiz_type}_{difficulty}_{ts}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # Individual .md
    for q in quizzes:
        qid = q["id"]
        md = f"---\n"
        md += f"type: Generated_Quiz\n"
        md += f"quiz_type: {q['type']}\n"
        md += f"difficulty: {difficulty}\n"
        if q["key_concepts"]:
            md += f"key_concepts: {json.dumps(q['key_concepts'], ensure_ascii=False)}\n"
        if q["source"]:
            md += f"source: {q['source']}\n"
        md += f"---\n\n"
        md += f"## Question {qid}\n\n"
        md += f"Q. {q['question']}\n\n"

        if q["type"] == "multiple_choice" and q["options"]:
            for opt in q["options"]:
                md += f"- {opt}\n"
            md += "\n"

        md += f"**Answer:** {q['answer']}\n\n"
        md += f"### Explanation\n\n{q['explanation']}\n"

        if q["key_concepts"]:
            md += f"\n### Key Concepts\n\n"
            for kc in q["key_concepts"]:
                md += f"- [[{kc}]]\n" if not kc.startswith("[[") else f"- {kc}\n"

        (gen_dir / f"GQ_{quiz_type}_{difficulty}_{ts}_Q{qid}.md").write_text(md, encoding="utf-8")

    print(f"  💾 Saved: {gen_dir}/ ({len(quizzes)} .md + 1 .json)")


def generate_quiz(cfg, book_name, n=5, quiz_type="multiple_choice",
                  source="random", difficulty="medium", save=True, print_result=True):
    """Generate quizzes from Vault's Summaries/Quizzes .md files.

    Reads:  Vault/{book}/Summaries/*.md, Quizzes/*.md
    Writes: Vault/{book}/Generated_Quizzes/
    """
    vault_dir = cfg.book_vault_dir(book_name)
    if not vault_dir.exists():
        raise FileNotFoundError(f"Vault not found: {vault_dir}")
    if quiz_type not in QUIZ_GEN_TYPES:
        raise ValueError(f"Unsupported type: {quiz_type}. Available: {list(QUIZ_GEN_TYPES.keys())}")

    model_config = cfg.get("quiz_generator.model", cfg.get("pipeline.quiz_create"))

    print(f"\n🎯 Quiz Generation")
    print(f"   Type: {quiz_type} | N: {n} | Difficulty: {difficulty} | Source: {source}")
    print(f"   Model: {model_config}")
    print(f"   Vault: {vault_dir}")

    # 1. Pick source
    src_type, src_text, source_files = _pick_source(vault_dir, source)

    # 2. Build prompt
    system = f"{QUIZ_GEN_SYSTEM}\n\nFormat per question:\n{QUIZ_GEN_TYPES[quiz_type]}"
    diff_desc = QUIZ_GEN_DIFFICULTY.get(difficulty, QUIZ_GEN_DIFFICULTY["medium"])
    src_instr = QUIZ_GEN_SOURCE_INSTR.get(src_type, QUIZ_GEN_SOURCE_INSTR["summary"])
    user = (
        f"{src_instr}\n\n"
        f"Type: {quiz_type}\n"
        f"Number of questions: {n}\n"
        f"Difficulty: {difficulty} — {diff_desc}\n"
        f"Source files: {', '.join(source_files)}\n\n"
        f"--- Source Material ---\n{src_text}"
    )

    # 3. LLM call
    client = OpenAI()
    print("  🔄 Calling LLM...")
    try:
        resp = client.chat.completions.create(
            **model_config,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        raw = JSONParser.parse(resp.choices[0].message.content)
    except Exception as e:
        print(f"  ❌ LLM error: {e}")
        return {"meta": {}, "quizzes": []}

    if not raw:
        print("  ❌ Parse failed")
        return {"meta": {}, "quizzes": []}

    # 4. Parse & normalize
    raw_quizzes = raw.get("quizzes", [])
    if not raw_quizzes and isinstance(raw, list):
        raw_quizzes = raw

    quizzes = [_normalize_quiz(q, i + 1, quiz_type, source_files)
               for i, q in enumerate(raw_quizzes)]

    result = {
        "meta": {
            "source_type": src_type,
            "source_files": source_files,
            "quiz_type": quiz_type,
            "n": len(quizzes),
            "difficulty": difficulty,
            "model": model_config.get("model", "unknown"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "quizzes": quizzes,
    }

    print(f"  ✅ {len(quizzes)} questions generated")

    # 5. Print
    if print_result:
        _print_quizzes(quizzes)

    # 6. Save
    if save and quizzes:
        _save_quizzes(quizzes, result, vault_dir, quiz_type, difficulty)

    return result