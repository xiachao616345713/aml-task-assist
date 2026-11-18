"""基于现有前后端表单的保守分级。模型不能提升自动办理权限。"""

from typing import Any

# 按公开业务流程标识配置办理边界。
POLICIES: dict[str, tuple[str, str]] = {
    "risk": ("按条分级", "中低风险可一键办理；高风险需要人工核查和材料"),
    "bnfValidate": ("人工复杂", "多轮预核验、BOMIS 核验、差异处理和差异报告"),
    "client": ("辅助判断", "可疑程度、交易判断依据及排除理由需要核实"),
    "cssReport": ("辅助判断", "接续可疑交易需要结合历史与当前交易判断"),
    "due": ("人工材料", "尽职调查需要核实调查结果与附件"),
    "black": ("人工选择", "黑名单确认、排除、高风险和可疑参与存在多项选择"),
    "special": ("人工选择", "政要匹配需要核实真实命中与处置结论"),
    "blackRelieve": ("人工选择", "解除黑名单需要核实依据"),
    "blackAdd": ("人工选择", "新增黑名单需要核实依据"),
    "fund": ("辅助判断", "产品风险评级需要核实评级依据"),
    "busi": ("辅助判断", "业务风险评级需要核实评级依据"),
    "channel": ("辅助判断", "渠道风险评级需要核实评级依据"),
    "acco": ("人工核查", "客户信息维护需核对变更内容"),
    "judicial": ("人工材料", "司法查冻扣涉及法律文书、业务类型和风险重评"),
    "corporate": ("辅助判断", "三方数据查询结果需核实匹配结论"),
    "crop": ("人工核查", "受益人变更需核实变更信息与依据"),
    "client1": ("人工材料", "可疑交易上报需核对报送内容"),
    "beneficiary": ("人工材料", "受益所有人上报需核对申报内容与材料"),
    "exportDataApply": ("人工核查", "导出申请涉及申请单据、导出文件与审批意见"),
    "bnfAssociation": ("人工选择", "受益所有人关联关系需要核实"),
}


def policy_for(process_type: str) -> dict:
    level, reason = POLICIES.get(process_type, ("未知流程", "尚未核对表单与提交条件"))
    return {"level": level, "reason": reason,
            "one_click": False, "conditional_one_click": process_type == "risk"}


def risk_grade(record: dict, levels: list[dict]) -> dict:
    """等级按风险字典和明细双重核对；任何歧义都转人工。"""
    code = str(record.get("riskLev") or "").strip()
    matches = [item for item in levels if str(item.get("riskno") or "").strip() == code]
    if len(matches) != 1 or not code:
        return {"grade": "未知", "can_one_click": False, "reason": "风险等级字典未唯一匹配"}
    name = str(matches[0].get("memo") or "").strip()
    displayed = str(record.get("memo") or "").strip()
    if name not in ("低风险", "中风险", "高风险") or displayed != name:
        return {"grade": "未知", "can_one_click": False, "reason": "风险等级与页面显示不一致"}
    if name == "高风险":
        return {"grade": name, "can_one_click": False, "reason": "高风险须人工核查和补充材料"}
    return {"grade": name, "can_one_click": True, "reason": "风险等级已由字典和待办记录交叉核对"}


def summarize(rows: list[dict]) -> dict[str, Any]:
    items = []
    for row in rows:
        count = row.get("taskNum")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("待办数量无效")
        items.append({
            "process_type": row.get("processType"),
            "name": row.get("processTypeName"),
            "count": count,
            "overdue": row.get("overdueNum"),
            "policy": policy_for(str(row.get("processType") or "")),
        })
    return {"total": sum(item["count"] for item in items), "by_type": items}
