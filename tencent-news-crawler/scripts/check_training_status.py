import json

from service.fastgpt_training_service import (
    FastGPTTrainingService,
)

def main() -> int:
    try:
        result = (
            FastGPTTrainingService()
            .check_all()
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )

    if result["status"] == "error":
        return 1
    if result["status"] == "training":
        return 2
    
    return 0

if __name__ == "__main__":
    raise SystemExit(main())