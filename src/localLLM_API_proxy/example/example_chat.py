"""채팅을 호출하고 전체 응답과 최종 답변을 확인한다."""
import json
import sys

from llama_wrapper import Llama

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

with Llama(model_path="models/gemma.gguf", n_ctx=65536) as llm:
    # 로컬 원본 라이브러리에는 서버 전용 템플릿 옵션을 전달하지 않는다.
    options = {"chat_template_kwargs": {"enable_thinking": False}} if getattr(llm, "backend", "local") == "remote" else {}
    response = llm.create_chat_completion(
        messages=[{"role": "user", "content": "대한민국의 수도를 한 문장으로 알려줘."}],
        max_tokens=512, temperature=0.2, **options,
    )
    print(json.dumps(response, ensure_ascii=False, indent=2))
    print("답변:", response["choices"][0]["message"].get("content"))
