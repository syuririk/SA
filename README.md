# Study Assistant for Obsidian

PDF 교재를 OCR → 청킹 → 요약/퀴즈 생성하여 Obsidian vault로 변환하는 파이프라인.

## 구조

```
config.yaml        설정 파일
src/
  config.py        Config 클래스
  prompts.py       시스템 프롬프트 (4종)
  utils.py         공용 유틸리티
  ocr.py           Mistral OCR (배치 분할)
  chunking.py      LLM 자동 청킹
  pipeline.py      요약 + 퀴즈 → Obsidian vault
```

## 파이프라인

```
PDF → [ocr.py] → page_0001.md ~ page_XXXX.md
    → [chunking.py] → chunks.json
    → [pipeline.py] → obsidian_output/
                        ├── Master_ToC.md
                        ├── Sources/
                        ├── Summaries/
                        └── Quizzes/
```

## 설정

`config.yaml`에서 전체 설정을 관리합니다.

```yaml
paths:
  drive_base: "/path/to/study_data"

ocr:
  batch_size: 50
  max_concurrent: 3

chunking:
  mode: "auto"
  model:
    model: "gpt-4.1-nano"
    temperature: 0.2

pipeline:
  summary:
    model: "gpt-5-mini"
  quiz_extract:
    model: "gpt-5-mini"
  quiz_create:
    model: "gpt-5-mini"
```

모델 dict는 OpenAI API에 `**config`로 직접 전달됩니다. 

## Config 클래스

```python
from src import Config

cfg = Config("config.yaml")

cfg.show()                           # 전체 출력
cfg.show("pipeline")                 # 섹션별 출력
cfg.get("pipeline.summary.model")    # 개별 조회

cfg.set("book.index", 2)             # 수정
cfg.set("pipeline.quiz_create", {"model": "o4-mini"})

cfg.save("my_config.yaml")           # 저장
```

## 청크 타입

| 타입 | 처리 |
|---|---|
| `toc` | 원본만 저장 |
| `heading` | 원본만 저장 |
| `content` | 요약 + 퀴즈 생성 |
| `quiz` | 퀴즈 추출만 |

## 사전 준비

- Mistral API 키 — [console.mistral.ai](https://console.mistral.ai)
- OpenAI API 키 — [platform.openai.com](https://platform.openai.com)

## 라이선스

MIT