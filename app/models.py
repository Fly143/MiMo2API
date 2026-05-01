"""OpenAI API 数据模型"""

from typing import List, Optional, Literal, Any, Dict
from pydantic import BaseModel, Field


class OpenAIMessage(BaseModel):
    """OpenAI消息"""
    role: str
    content: Optional[Any] = None  # str or List[Dict] for multimodal
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class OpenAITool(BaseModel):
    """OpenAI工具定义"""
    type: str = "function"
    function: Dict[str, Any]


class OpenAIRequest(BaseModel):
    """OpenAI请求"""
    model: str
    messages: List[OpenAIMessage]
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    reasoning_effort: Optional[str] = Field(None, description="深度思考等级: low/medium/high")
    tools: Optional[List[OpenAITool]] = None
    tool_choice: Optional[Any] = None


class OpenAIDelta(BaseModel):
    """OpenAI流式响应增量"""
    role: Optional[str] = None
    content: Optional[str] = None
    reasoning: Optional[str] = Field(None, description="深度思考内容 (OpenAI o1 格式)")
    reasoning_content: Optional[str] = Field(None, description="深度思考内容 (DeepSeek 格式)")
    tool_calls: Optional[List[Dict[str, Any]]] = None


class OpenAIChoice(BaseModel):
    """OpenAI选择项"""
    index: int
    message: Optional[OpenAIMessage] = None
    delta: Optional[OpenAIDelta] = None
    finish_reason: Optional[str] = None


class OpenAIUsage(BaseModel):
    """OpenAI使用统计"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class OpenAIResponse(BaseModel):
    """OpenAI响应"""
    id: str
    object: str
    created: int
    model: str
    choices: List[OpenAIChoice]
    usage: Optional[OpenAIUsage] = None


class ParseCurlRequest(BaseModel):
    """解析cURL请求"""
    curl: str


class TestAccountRequest(BaseModel):
    """测试账号请求"""
    service_token: str
    user_id: str
    xiaomichatbot_ph: str
