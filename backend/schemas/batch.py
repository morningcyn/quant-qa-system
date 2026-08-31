# 批量评分 API 请求模型
from pydantic import BaseModel, Field


class RoomImport(BaseModel):
    """Excel「房间对话」导出一行：一个房间（客户会话）的完整聊天记录。"""

    customer_name: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)


class BatchImportIn(BaseModel):
    """批量导入：整批聊天记录（含多个客户会话），服务端自动切分。

    raw_text 模式：文本粘贴/上传，按客户昵称自动切分；
    rooms 模式：Excel 逐房间导出（每行一个房间的完整聊天记录），每个房间独立会话。
    """

    raw_text: str | None = Field(default=None, min_length=1)
    rooms: list[RoomImport] | None = Field(default=None, max_length=5000)
    title: str | None = Field(default=None, max_length=200)
