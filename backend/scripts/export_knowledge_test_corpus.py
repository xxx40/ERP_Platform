import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "backend" / "tests" / "fixtures" / "phase1_knowledge_cases.json"
OUTPUT = ROOT / "docs" / "testing" / "phase1-knowledge-corpus"


def main() -> None:
    cases = json.loads(SOURCE.read_text(encoding="utf-8"))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for case in cases:
        questions = "\n".join(f"- {question}" for question in case["questions"])
        expected = "\n".join(f"- {point}" for point in case["expected_points"])
        document = (
            f"# {case['title']}\n\n"
            f"- 案例编号：`{case['id']}`\n"
            f"- 业务阶段：`{case['business_stage']}`\n"
            f"- 唯一标识：`{case['unique_marker']}`\n"
            "- 数据属性：合成测试资料，不代表公司正式制度\n\n"
            f"## 案例内容\n\n{case['content']}\n\n"
            f"## 测试问题\n\n{questions}\n\n"
            f"## 预期要点\n\n{expected}\n"
        )
        (OUTPUT / f"{case['id']}-{case['business_stage']}.md").write_text(
            document,
            encoding="utf-8",
        )
    print(f"已导出 {len(cases)} 份测试文档到 {OUTPUT}")


if __name__ == "__main__":
    main()
