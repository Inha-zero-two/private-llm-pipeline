"""JSON 스키마에 맞춘 응답을 생성한다."""
import json
import sys

from llama_wrapper import Llama

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

with Llama(model_path="models/gemma.gguf", n_ctx=65536) as llm:
    # 로컬 원본 라이브러리에는 서버 전용 템플릿 옵션을 전달하지 않는다.
    options = {"chat_template_kwargs": {"enable_thinking": False}} if getattr(llm, "backend", "local") == "remote" else {}
    response = llm.create_chat_completion(
        messages=[{"role": "user", "content": "한국의 수도와 국가 이름을 JSON으로 알려줘."}],
        response_format={"type": "json_object", "schema": {
            "type": "object", "properties": {"country": {"type": "string"}, "capital": {"type": "string"}},
            "required": ["country", "capital"], "additionalProperties": False,
        }},
        max_tokens=512, temperature=0, **options,
    )
    print(json.dumps(response, ensure_ascii=False, indent=2))
    print("구조화된 답변:", json.loads(response["choices"][0]["message"]["content"]))
