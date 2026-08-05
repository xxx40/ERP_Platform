from app.core.security import extract_order_number, is_high_risk_request


def test_descriptive_audit_question_is_not_rejected() -> None:
    assert not is_high_risk_request("采购订单审核后应该怎么完成入库？")
    assert not is_high_risk_request("PO202607003 审核状态")
    assert not is_high_risk_request("请查询 PO202607003 是否审核通过")
    assert not is_high_risk_request("PO202607003 的审核时间是什么时候？")


def test_write_operation_is_rejected() -> None:
    assert is_high_risk_request("帮我审核这张采购订单")
    assert is_high_risk_request("把采购订单 PO202607001 删除")
    assert is_high_risk_request("帮我把PO202607001订单删掉")
    assert is_high_risk_request("撤销 PO202607001")
    assert is_high_risk_request("取消采购订单 PO202607001")
    assert is_high_risk_request("PO202607001 执行作废")


def test_extract_order_number() -> None:
    assert extract_order_number("请查 PO202607001 的状态") == "PO202607001"
    assert extract_order_number("PO202607001下一步应该做什么") == "PO202607001"
    assert extract_order_number("采购订单po202607001下一步做什么") == "PO202607001"
    assert extract_order_number("查询 PO-202607001") == "PO202607001"
    assert extract_order_number("查询 PO_202607001") == "PO202607001"
    assert extract_order_number("查询 ＰＯ：２０２６０７００１") == "PO202607001"


def test_order_number_does_not_match_inside_another_identifier() -> None:
    assert extract_order_number("XPO202607001") is None
    assert extract_order_number("PO202607001A") is None
