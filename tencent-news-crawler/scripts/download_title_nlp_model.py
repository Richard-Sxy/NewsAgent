import argparse
import json

from config import settings
from intelligence.title_nlp import LTPTitleAnalyzer


def main() -> None:
    parser = argparse.ArgumentParser(description="下载并验证本地标题 NLP 模型")
    parser.add_argument("--model", default=settings.title_nlp_model)
    parser.add_argument("--cache-dir", default=settings.title_nlp_cache_dir)
    parser.add_argument(
        "--title",
        default="雷军宣布小米集团与英伟达合作推出澎湃OS新品",
    )
    args = parser.parse_args()

    analyzer = LTPTitleAnalyzer(
        model_name=args.model,
        cache_dir=args.cache_dir,
        local_files_only=False,
        lexicon_path=settings.title_nlp_lexicon_path,
    )
    analysis = analyzer.analyze(args.title)
    print(json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
