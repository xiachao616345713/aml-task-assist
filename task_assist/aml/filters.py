"""待办接口的查询白名单；归属和机构范围由业务服务按登录身份决定。"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class RiskFilters(BaseModel):
    riskLev: str = Field(default="", max_length=100)

    def params(self) -> dict:
        return {k: v.strip() for k, v in self.model_dump().items() if v and v.strip()}


class BnfFilters(BaseModel):
    custId: str = Field(default="", max_length=128)
    custName: str = Field(default="", max_length=200)
    certCode: str = Field(default="", max_length=128)
    certType: str = Field(default="", max_length=32)
    taskName: str = Field(default="", max_length=300)
    status: str = Field(default="", max_length=32)
    disCode: str = Field(default="", max_length=300)
    disType: Literal["", "1", "2"] = ""
    bomisCheckId: str = Field(default="", max_length=128)
    preCheckStatus: str = Field(default="", max_length=100)
    bomisCheckStatus: str = Field(default="", max_length=100)
    diffBizState: str = Field(default="", max_length=32)
    diffReportResult: str = Field(default="", max_length=100)
    isOpenedAccount: Literal["", "0", "1"] = ""
    remainingDays: int | None = Field(default=None, ge=0, le=100000)
    collectDateBegin: date | None = None
    collectDateEnd: date | None = None
    column: Literal["", "remainingDays", "collectDate"] = ""
    order: Literal["", "asc", "desc"] = ""

    def params(self) -> dict:
        if self.collectDateBegin and self.collectDateEnd and self.collectDateBegin > self.collectDateEnd:
            raise ValueError("采集开始日期不能晚于结束日期")
        return {k: str(v).strip() for k, v in self.model_dump().items()
                if v is not None and str(v).strip()}
