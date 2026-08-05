import re
import unicodedata


WRITE_ACTIONS = (
    "审核",
    "反审核",
    "提交",
    "修改",
    "更改",
    "删除",
    "删掉",
    "移除",
    "关闭",
    "作废",
    "取消",
    "撤销",
    "新增",
    "创建",
)

HIGH_RISK_PATTERN = re.compile(
    r"(帮我|请|替我|直接|自动|给我|我要).{0,16}"
    r"(审核|反审核|提交|修改|更改|删除|删掉|移除|关闭|作废|取消|撤销|新增|创建)|"
    r"(把|将).{0,30}(采购订单|订单|单据|PO[\s_:/-]?\d+).{0,40}"
    r"(审核|反审核|提交|修改|更改|删除|删掉|移除|关闭|作废|取消|撤销)|"
    r"(审核|反审核|提交|修改|删除|删掉|关闭|作废|取消|撤销)\s*"
    r"(一下|这张|这个|该|编号为|采购订单|订单|单据|PO[-_]?\d+)|"
    r"(采购订单|订单|单据|PO[\s_:/-]?\d+).{0,20}"
    r"(审核|反审核|提交|修改|更改|删除|删掉|移除|关闭|作废|取消|撤销)"
    r"(?!状态|结果|记录|进度|时间|日期|人员|人|了吗|成功|通过|完成|后|前|流程|规则|要求|说明)|"
    r"(执行|立即|现在).{0,10}"
    r"(审核|反审核|提交|修改|更改|删除|删掉|移除|关闭|作废|取消|撤销)",
    re.IGNORECASE,
)

ORDER_NUMBER_PATTERN = re.compile(
    r"(?<![A-Z0-9])PO[\s_:/-]?(\d{6,})(?![A-Z0-9])",
    re.IGNORECASE,
)


def is_high_risk_request(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", message)
    return bool(HIGH_RISK_PATTERN.search(normalized))


def extract_order_number(message: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", message)
    match = ORDER_NUMBER_PATTERN.search(normalized)
    return f"PO{match.group(1)}" if match else None
