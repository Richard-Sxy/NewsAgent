"""使用人工文章对标注评测事件聚类结果。"""

import argparse
import json
from pathlib import Path

from intelligence.evaluation import evaluate_pairs
from intelligence.schemas import EventDataset, EventPairAnnotation


def main() -> int:
    parser = argparse.ArgumentParser(description="评测新闻事件聚类")
    parser.add_argument("--clusters", required=True)
    parser.add_argument("--annotations", required=True)
    args = parser.parse_args()

    dataset = EventDataset.model_validate_json(
        Path(args.clusters).read_text(encoding="utf-8")
    )
    annotations = [
        EventPairAnnotation.model_validate(json.loads(line))
        for line in Path(args.annotations).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    print(json.dumps(evaluate_pairs(dataset, annotations), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
